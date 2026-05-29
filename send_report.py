#!/usr/bin/env python3

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import html
import json
import math
import os
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo


USER_AGENT = "Mozilla/5.0 (HK Tech Preopen Bot)"
HTTP_TIMEOUT_SECONDS = 12
MELBOURNE_TZ = ZoneInfo("Australia/Melbourne")
REPORT_TITLE_PREFIX = "# 恒科期货盘前｜"
MAX_TG_LEN = 3500

QUOTE_SYMBOLS = {
    "Nasdaq": "^IXIC",
    "QQQ": "QQQ",
    "S&P 500": "^GSPC",
    "SMH": "SMH",
    "KWEB": "KWEB",
    "BABA": "BABA",
    "JD": "JD",
    "BIDU": "BIDU",
    "PDD": "PDD",
    "BILI": "BILI",
    "TCEHY": "TCEHY",
    "NTES": "NTES",
    "TCOM": "TCOM",
    "XPEV": "XPEV",
    "LI": "LI",
    "NIO": "NIO",
    "Nikkei 225": "^N225",
    "TOPIX": "^TOPX",
    "KOSPI": "^KS11",
    "KOSDAQ": "^KQ11",
    "ASX 200": "^AXJO",
    "DXY": "DX-Y.NYB",
    "USD/CNH": "CNH=X",
    "USD/JPY": "JPY=X",
    "AUD/USD": "AUDUSD=X",
    "US10Y": "^TNX",
}

HK_PROXY_NOTES = {
    "0700.HK / 腾讯": "TCEHY ADR 代理",
    "9988.HK / 阿里": "BABA ADR 代理",
    "3690.HK / 美团": "暂无可靠美股盘前代理",
    "9618.HK / 京东": "JD ADR 代理",
    "1810.HK / 小米": "暂无可靠美股盘前代理",
    "1024.HK / 快手": "暂无可靠美股盘前代理",
    "9999.HK / 网易": "NTES ADR 代理",
    "9888.HK / 百度": "BIDU ADR 代理",
    "9961.HK / 携程": "TCOM ADR 代理",
    "9626.HK / 哔哩哔哩": "BILI ADR 代理",
    "9868.HK / 小鹏": "XPEV ADR 代理",
    "2015.HK / 理想": "LI ADR 代理",
    "9866.HK / 蔚来": "NIO ADR 代理",
}


@dataclass
class QuoteSnapshot:
    symbol: str
    label: str
    close: float
    previous_close: float
    high: Optional[float]
    low: Optional[float]
    history: List[Tuple[int, float]]

    @property
    def change_pct(self) -> Optional[float]:
        if (
            self.close is None
            or self.previous_close is None
            or math.isnan(self.close)
            or math.isnan(self.previous_close)
            or not self.previous_close
        ):
            return None
        return (self.close / self.previous_close - 1.0) * 100.0

    def return_over(self, sessions_back: int) -> Optional[float]:
        if len(self.history) <= sessions_back:
            return None
        earlier = self.history[-(sessions_back + 1)][1]
        if (
            earlier is None
            or self.close is None
            or math.isnan(earlier)
            or math.isnan(self.close)
            or not earlier
        ):
            return None
        return (self.close / earlier - 1.0) * 100.0


def unavailable_snapshot(label: str, symbol: str) -> QuoteSnapshot:
    return QuoteSnapshot(
        symbol=symbol,
        label=label,
        close=float("nan"),
        previous_close=float("nan"),
        high=None,
        low=None,
        history=[],
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate the HK tech preopen Telegram report."
    )
    parser.add_argument("--env-file", default="", help="Optional local env file.")
    parser.add_argument(
        "--state-file",
        default=str(Path(__file__).with_name(".state") / "last_sent.json"),
        help="State file path.",
    )
    parser.add_argument(
        "--reports-dir",
        default=str(Path(__file__).with_name("reports")),
        help="Reports output directory.",
    )
    parser.add_argument("--force", action="store_true", help="Send even if already sent today.")
    parser.add_argument(
        "--schedule-guard",
        action="store_true",
        help="Only run near Melbourne 10:20 on weekdays.",
    )
    parser.add_argument(
        "--send-test-message",
        action="store_true",
        help="Send a Telegram test message only.",
    )
    return parser.parse_args()


def merged_env(env_file_arg: str) -> Dict[str, str]:
    values = dict(os.environ)
    if env_file_arg:
        env_path = Path(env_file_arg).expanduser()
        if env_path.exists():
            for raw_line in env_path.read_text(encoding="utf-8").splitlines():
                line = raw_line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                values[key.strip()] = value.strip()
    return values


def load_state(path: Path) -> Dict[str, str]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def save_state(path: Path, payload: Dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def should_run_for_schedule() -> bool:
    now_local = datetime.now(MELBOURNE_TZ)
    if now_local.weekday() not in (0, 1, 2, 3, 4):
        return False
    minutes_now = now_local.hour * 60 + now_local.minute
    target = 10 * 60 + 20
    return abs(minutes_now - target) <= 20


def fetch_json(url: str) -> Dict:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
        return json.loads(response.read().decode("utf-8"))


def fetch_chart(symbol: str, label: str) -> QuoteSnapshot:
    url = (
        "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
        "?range=3mo&interval=1d&includePrePost=false&events=div,splits"
    ).format(symbol=urllib.parse.quote(symbol, safe=""))
    payload = fetch_json(url)
    result = payload["chart"]["result"][0]
    quote = result["indicators"]["quote"][0]
    timestamps = result.get("timestamp") or []
    closes = quote.get("close") or []
    highs = quote.get("high") or []
    lows = quote.get("low") or []

    history: List[Tuple[int, float]] = []
    for ts, close in zip(timestamps, closes):
        if close is None:
            continue
        history.append((ts, float(close)))

    if len(history) < 2:
        raise RuntimeError("Not enough history for {0}".format(symbol))

    latest_close = history[-1][1]
    previous_close = history[-2][1]
    high = float(highs[-1]) if highs and highs[-1] is not None else None
    low = float(lows[-1]) if lows and lows[-1] is not None else None

    return QuoteSnapshot(
        symbol=symbol,
        label=label,
        close=latest_close,
        previous_close=previous_close,
        high=high,
        low=low,
        history=history,
    )


def fetch_many(symbol_map: Dict[str, str]) -> Dict[str, QuoteSnapshot]:
    def load_one(item: Tuple[str, str]) -> Tuple[str, QuoteSnapshot]:
        label, symbol = item
        try:
            return label, fetch_chart(symbol, label)
        except Exception:
            return label, unavailable_snapshot(label, symbol)

    snapshots: Dict[str, QuoteSnapshot] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        for label, snap in executor.map(load_one, symbol_map.items()):
            snapshots[label] = snap
    return snapshots


def fmt_num(value: Optional[float], digits: int = 2) -> str:
    if value is None or math.isnan(value):
        return "暂无可靠数据"
    return "{0:,.{1}f}".format(value, digits)


def fmt_pct(value: Optional[float], digits: int = 2) -> str:
    if value is None or math.isnan(value):
        return "暂无可靠数据"
    sign = "+" if value >= 0 else ""
    return "{0}{1:.{2}f}%".format(sign, value, digits)


def fmt_yield_tnx(snapshot: QuoteSnapshot) -> str:
    if snapshot.close is None or math.isnan(snapshot.close):
        return "暂无可靠数据"
    return "{0:.2f}%".format(snapshot.close / 10.0)


def support_resistance(snapshot: QuoteSnapshot) -> Tuple[str, str]:
    prices = [value for _, value in snapshot.history[-15:]]
    if len(prices) < 5:
        return "暂无可靠数据", "暂无可靠数据"
    return fmt_num(min(prices)), fmt_num(max(prices))


def quote_ok(snapshot: QuoteSnapshot) -> bool:
    return snapshot.change_pct is not None


def data_completeness(quotes: Dict[str, QuoteSnapshot]) -> Tuple[bool, List[str], List[str]]:
    reasons = []
    confirmed = []

    required = ["Nasdaq", "QQQ", "S&P 500", "US10Y", "DXY", "USD/CNH", "KWEB", "Nikkei 225", "KOSPI", "ASX 200"]
    for key in required:
        if quote_ok(quotes[key]):
            confirmed.append(key)
        else:
            reasons.append("{0} 暂无可靠数据".format(key))

    if not quote_ok(quotes["TOPIX"]):
        reasons.append("TOPIX 暂无可靠数据")
    if not quote_ok(quotes["KOSDAQ"]):
        reasons.append("KOSDAQ 暂无可靠数据")

    adr_keys = ["BABA", "JD", "BIDU", "PDD", "BILI", "TCEHY", "NTES", "TCOM"]
    adr_available = sum(1 for key in adr_keys if quote_ok(quotes[key]))
    if adr_available >= 4:
        confirmed.append("中概 / ADR")
    else:
        reasons.append("中概 / ADR 数据不完整")

    reasons.append("恒指夜期 / 恒科夜期 暂无统一免费可靠源，采用 ADR / ETF 代理")

    enough = len([r for r in reasons if "暂无统一免费可靠源" not in r]) == 0
    return enough, confirmed, reasons


def score_bucket(score: int) -> str:
    if score >= 20:
        return "中强"
    if score >= 15:
        return "中性"
    if score >= 10:
        return "弱"
    return "不交易"


def direction_label(total_score: int) -> Tuple[str, str, str]:
    if total_score >= 20:
        return "偏多", "中高", "温和偏多"
    if total_score >= 15:
        return "震荡", "中", "外围强但需验证"
    if total_score >= 10:
        return "震荡", "中低", "震荡等待确认"
    return "偏空", "中高", "温和偏空"


def telegram_chunks(messages: List[str]) -> List[str]:
    output = []
    for message in messages:
        if len(message) <= MAX_TG_LEN:
            output.append(message)
            continue
        parts = []
        current = ""
        for block in message.split("\n\n"):
            candidate = block if not current else current + "\n\n" + block
            if len(candidate) <= MAX_TG_LEN:
                current = candidate
            else:
                if current:
                    parts.append(current)
                current = block
        if current:
            parts.append(current)
        output.extend(parts)
    return output


def telegram_request(token: str, method: str, payload: Dict[str, str]) -> Dict:
    data = urllib.parse.urlencode(payload).encode("utf-8")
    request = urllib.request.Request(
        url="https://api.telegram.org/bot{0}/{1}".format(token, method),
        data=data,
        headers={
            "User-Agent": USER_AGENT,
            "Content-Type": "application/x-www-form-urlencoded",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
        return json.loads(response.read().decode("utf-8"))


def send_messages(token: str, chat_id: str, messages: List[str]) -> None:
    chunks = telegram_chunks(messages)
    total = len(chunks)
    for index, message in enumerate(chunks, 1):
        body = message
        if total > len(messages):
            body = "[{0}/{1}]\n{2}".format(index, total, message)
        result = telegram_request(
            token,
            "sendMessage",
            {
                "chat_id": chat_id,
                "text": body,
                "disable_web_page_preview": "true",
            },
        )
        if not result.get("ok"):
            raise RuntimeError("Telegram send failed: {0}".format(result))


def send_failure_alert(token: str, chat_id: str, error_text: str) -> None:
    try:
        send_messages(
            token,
            chat_id,
            [
                "⚠️ 恒科期货盘前 Bot 失败\n时间：{0}\n原因：{1}\n说明：本次未成功生成盘前日报。".format(
                    datetime.now(MELBOURNE_TZ).isoformat(timespec="seconds"),
                    error_text[:1000],
                )
            ],
        )
    except Exception:
        pass


def render_insufficient(report_date: str, confirmed: List[str], reasons: List[str]) -> List[str]:
    message = [
        "⚠️ 数据不足 / 不适合盘前交易｜{0}".format(report_date),
        "",
        "结论：",
        "当前不适合给出明确恒科期货方向。",
        "",
        "原因：",
    ]
    for idx, reason in enumerate(reasons[:6], 1):
        message.append("{0}. {1}".format(idx, reason))
    message.extend(
        [
            "",
            "已确认数据：",
            "- {0}".format("、".join(confirmed) if confirmed else "暂无可靠数据"),
            "",
            "交易建议：",
            "- 盘前不建仓 / 轻仓观察",
            "- 等日本、韩国、汇率、ADR进一步确认",
            "",
            "来源：",
            "- Yahoo Finance: https://finance.yahoo.com/",
        ]
    )
    return ["\n".join(message)]


def build_report(quotes: Dict[str, QuoteSnapshot]) -> Tuple[str, List[str]]:
    local_now = datetime.now(MELBOURNE_TZ)
    report_date = local_now.date().isoformat()
    title = "{0}{1}".format(REPORT_TITLE_PREFIX, report_date)

    qqq = quotes["QQQ"]
    nasdaq = quotes["Nasdaq"]
    kweb = quotes["KWEB"]
    us10y = quotes["US10Y"]
    dxy = quotes["DXY"]
    usdcnh = quotes["USD/CNH"]
    nikkei = quotes["Nikkei 225"]
    topix = quotes["TOPIX"]
    kospi = quotes["KOSPI"]
    kosdaq = quotes["KOSDAQ"]
    asx = quotes["ASX 200"]

    score_us = 4 if (nasdaq.change_pct or 0) > 0 and (qqq.change_pct or 0) > 0 else 2
    score_cn = 4 if (kweb.change_pct or 0) > 0 and sum((quotes[k].change_pct or -99) > 0 for k in ["BABA", "JD", "BIDU", "TCEHY"]) >= 2 else 2
    score_asia = 4 if (nikkei.change_pct or 0) > 0 and (kospi.change_pct or 0) > 0 and (asx.change_pct or 0) > 0 else 2
    score_fx = 4 if (usdcnh.change_pct or 99) <= 0.2 and (dxy.change_pct or 99) <= 0.5 else 2
    hk_positive = sum((quotes[k].change_pct or -99) > 0 for k in ["TCEHY", "BABA", "JD", "NTES", "BIDU", "TCOM", "BILI", "XPEV", "LI", "NIO"])
    score_hk = 4 if hk_positive >= 5 else 2
    total_score = score_us + score_cn + score_asia + score_fx + score_hk
    direction, confidence, state = direction_label(total_score)

    position = "中高" if total_score >= 20 else "中" if total_score >= 15 else "中低" if total_score >= 10 else "低"
    one_liner = "美股{0}，亚洲{1}，人民币{2}，恒科今日更偏向{3}。".format(
        "偏强" if (nasdaq.change_pct or 0) > 0 else "偏弱",
        "确认偏强" if (nikkei.change_pct or 0) > 0 and (kospi.change_pct or 0) > 0 else "分化",
        "稳定" if (usdcnh.change_pct or 99) <= 0.2 else "承压",
        direction,
    )

    hk_positive_lines = []
    hk_negative_lines = []
    weight_proxy_map = {
        "腾讯": "TCEHY",
        "阿里": "BABA",
        "京东": "JD",
        "网易": "NTES",
        "百度": "BIDU",
        "携程": "TCOM",
        "哔哩哔哩": "BILI",
        "小鹏": "XPEV",
        "理想": "LI",
        "蔚来": "NIO",
    }
    for label, symbol in weight_proxy_map.items():
        change = quotes[symbol].change_pct
        if change is None:
            continue
        item = "- {0}：{1}（{2}）".format(label, fmt_pct(change), HK_PROXY_NOTES.get("{0}.HK / {1}".format("0000", label), "ADR代理"))
        if change >= 0:
            hk_positive_lines.append(item)
        else:
            hk_negative_lines.append(item)

    message1 = "\n".join(
        [
            title,
            "",
            "## 1. 🚦盘前总信号",
            "方向：{0}".format(direction),
            "信心：{0}".format(confidence),
            "仓位：{0}".format(position),
            "状态：{0}".format(state),
            "",
            "一句话：",
            one_liner,
            "",
            "## 2. ⚡1分钟核心结论",
            "1. Nasdaq：{0}，美股科技{1}。".format(fmt_pct(nasdaq.change_pct), "偏强" if (nasdaq.change_pct or 0) > 0 else "偏弱"),
            "2. QQQ：{0}，方向与Nasdaq {1}。".format(fmt_pct(qqq.change_pct), "一致" if (qqq.change_pct or 0) * (nasdaq.change_pct or 0) >= 0 else "分化"),
            "3. KWEB：{0}，相对QQQ {1}。".format(fmt_pct(kweb.change_pct), "偏强" if (kweb.change_pct or -99) > (qqq.change_pct or -99) else "偏弱"),
            "4. 中概ADR：BABA {0} / JD {1} / BIDU {2}。".format(fmt_pct(quotes["BABA"].change_pct), fmt_pct(quotes["JD"].change_pct), fmt_pct(quotes["BIDU"].change_pct)),
            "5. 10Y美债：{0}，对成长股{1}。".format(fmt_yield_tnx(us10y), "偏友好" if (us10y.change_pct or 99) <= 0 else "偏压制"),
            "6. DXY：{0}，美元{1}。".format(fmt_pct(dxy.change_pct), "偏稳" if (dxy.change_pct or 99) <= 0.5 else "偏强"),
            "7. USD/CNH：{0}，人民币{1}。".format(fmt_num(usdcnh.close, 4), "稳定" if (usdcnh.change_pct or 99) <= 0.2 else "偏弱"),
            "8. 日本/韩国/澳洲：{0} / {1} / {2}。".format(fmt_pct(nikkei.change_pct), fmt_pct(kospi.change_pct), fmt_pct(asx.change_pct)),
            "9. 恒科判断：{0}，但需看港股开盘确认。".format(direction),
            "",
            "## 7. 🕒三阶段交易计划",
            "A. 开盘前3–4小时",
            "动作：{0}".format("做多" if total_score >= 20 else "观望" if total_score < 15 else "只等确认"),
            "仓位：{0}".format(position),
            "理由：外围信号{0}。".format("偏正面" if total_score >= 15 else "不够一致"),
            "取消条件：USD/CNH快速上冲 / 亚洲转弱。",
            "",
            "B. 开盘前30分钟",
            "动作：{0}".format("持有并观察" if total_score >= 20 else "不追 / 等开盘"),
            "重点看：恒科代理、USD/CNH、腾讯/阿里、日韩是否回落。",
            "",
            "C. 开盘后15–120分钟",
            "前15分钟：看是否站稳开盘价。",
            "前30分钟：看成交是否支持方向。",
            "前60分钟：判断是否继续持仓。",
            "1–2小时：优先止盈 / 止损 / 平仓。",
        ]
    )

    message2_lines = [
            "## 3. 📊核心数据快照",
            "美股：",
            "- Nasdaq：{0}".format(fmt_pct(nasdaq.change_pct)),
            "- QQQ：{0}".format(fmt_pct(qqq.change_pct)),
            "- S&P 500：{0}".format(fmt_pct(quotes["S&P 500"].change_pct)),
            "- SMH：{0}".format(fmt_pct(quotes["SMH"].change_pct)),
            "",
            "中概：",
            "- KWEB：{0}".format(fmt_pct(kweb.change_pct)),
            "- BABA：{0}".format(fmt_pct(quotes["BABA"].change_pct)),
            "- JD：{0}".format(fmt_pct(quotes["JD"].change_pct)),
            "- BIDU：{0}".format(fmt_pct(quotes["BIDU"].change_pct)),
            "- PDD：{0}".format(fmt_pct(quotes["PDD"].change_pct)),
            "- BILI：{0}".format(fmt_pct(quotes["BILI"].change_pct)),
            "",
            "亚洲：",
            "- Nikkei 225：{0}".format(fmt_pct(nikkei.change_pct)),
            "- TOPIX：{0}".format(fmt_pct(topix.change_pct)),
            "- KOSPI：{0}".format(fmt_pct(kospi.change_pct)),
            "- KOSDAQ：{0}".format(fmt_pct(kosdaq.change_pct)),
            "- ASX 200：{0}".format(fmt_pct(asx.change_pct)),
            "",
            "宏观：",
            "- 10Y美债：{0}".format(fmt_yield_tnx(us10y)),
            "- DXY：{0}".format(fmt_pct(dxy.change_pct)),
            "- USD/CNH：{0}".format(fmt_num(usdcnh.close, 4)),
            "- USD/JPY：{0}".format(fmt_num(quotes["USD/JPY"].close, 4)),
            "- AUD/USD：{0}".format(fmt_num(quotes["AUD/USD"].close, 4)),
            "",
            "港股参考：",
            "- 恒指夜期：暂无可靠数据",
            "- 恒科夜期：暂无可靠数据",
            "- 港股ADR：以腾讯/阿里/京东/网易/百度/携程/哔哩/新势力代理",
            "",
            "## 4. 🧭交易方向评分",
            "美股科技传导：{0}/5".format(score_us),
            "中概ADR支持：{0}/5".format(score_cn),
            "亚洲开盘确认：{0}/5".format(score_asia),
            "汇率环境：{0}/5".format(score_fx),
            "恒科权重一致性：{0}/5".format(score_hk),
            "综合方向分：{0}/25".format(total_score),
            "今日信号等级：{0}".format(score_bucket(total_score)),
            "",
            "## 5. 🌏外围传导判断",
            "美股 → 恒科：{0}｜原因：QQQ {1}。".format("正向" if score_us >= 4 else "中性/负向", fmt_pct(qqq.change_pct)),
            "中概 → 恒科：{0}｜原因：KWEB {1}。".format("正向" if score_cn >= 4 else "中性/负向", fmt_pct(kweb.change_pct)),
            "亚洲 → 恒科：{0}｜原因：日/韩/澳 {1}/{2}/{3}。".format(
                "正向" if score_asia >= 4 else "中性/负向",
                fmt_pct(nikkei.change_pct),
                fmt_pct(kospi.change_pct),
                fmt_pct(asx.change_pct),
            ),
            "汇率 → 恒科：{0}｜原因：USD/CNH {1}。".format("正向" if score_fx >= 4 else "负向", fmt_num(usdcnh.close, 4)),
            "",
            "## 6. 🧩恒科核心权重检查",
            "权重一致性：{0}".format("强" if score_hk >= 4 else "中" if score_hk >= 2 else "弱"),
            "偏多权重：",
    ]
    message2_lines.extend(hk_positive_lines[:4] if hk_positive_lines else ["- 暂无可靠数据"])
    message2_lines.append("偏空权重：")
    message2_lines.extend(hk_negative_lines[:4] if hk_negative_lines else ["- 暂无可靠数据"])
    message2_lines.extend(
        [
            "关键矛盾：",
            "- 美团 / 小米 / 快手缺少可靠美股盘前代理",
            "- 夜期暂无统一免费可靠源",
        ]
    )
    message2 = "\n".join(message2_lines)

    long_trigger = sum(
        [
            1 if (qqq.change_pct or 0) > 0 else 0,
            1 if (kweb.change_pct or 0) > 0 else 0,
            1 if (nikkei.change_pct or 0) > 0 else 0,
            1 if (kospi.change_pct or 0) > 0 else 0,
            1 if (asx.change_pct or 0) > 0 else 0,
            1 if (usdcnh.change_pct or 99) <= 0.2 else 0,
            1 if score_hk >= 4 else 0,
        ]
    )
    short_trigger = sum(
        [
            1 if (qqq.change_pct or 0) < 0 else 0,
            1 if (kweb.change_pct or 0) < 0 else 0,
            1 if (nikkei.change_pct or 0) < 0 else 0,
            1 if (kospi.change_pct or 0) < 0 else 0,
            1 if (kosdaq.change_pct or 0) < (kospi.change_pct or 0) else 0,
            1 if (asx.change_pct or 0) < 0 else 0,
            1 if (usdcnh.change_pct or 0) > 0.2 else 0,
            1 if (us10y.change_pct or 0) > 0 else 0,
        ]
    )
    hkf_support, hkf_resistance = support_resistance(kweb)
    qqq_support, qqq_resistance = support_resistance(qqq)

    message3 = "\n".join(
        [
            "## 8. ✅做多条件",
            "今日已满足：{0}/10".format(long_trigger),
            "做多条件：{0}".format("充分" if long_trigger >= 7 else "部分" if long_trigger >= 4 else "不充分"),
            "",
            "## 9. ❌做空 / 避险条件",
            "今日已触发：{0}/10".format(short_trigger),
            "风险等级：{0}".format("高" if short_trigger >= 7 else "中高" if short_trigger >= 5 else "中" if short_trigger >= 3 else "低"),
            "",
            "## 10. 🎯关键价位与触发器",
            "恒科代理（KWEB）：",
            "- 上方压力：{0}".format(hkf_resistance),
            "- 下方支撑：{0}".format(hkf_support),
            "- 多头触发：突破压力并站稳",
            "- 空头触发：跌破支撑且无法收复",
            "QQQ：",
            "- 多头确认：{0}".format(qqq_resistance),
            "- 风险信号：{0}".format(qqq_support),
            "USD/CNH：",
            "- 风险位：{0}".format(fmt_num(usdcnh.close * 1.002, 4) if quote_ok(usdcnh) else "暂无可靠数据"),
            "- 利好位：{0}".format(fmt_num(usdcnh.close * 0.998, 4) if quote_ok(usdcnh) else "暂无可靠数据"),
            "",
            "## 11. 🚫今日不追单条件",
            "1. Nasdaq涨，但KWEB跌。",
            "2. 美股强，但USD/CNH快速上行。",
            "3. 日本/韩国高开低走。",
            "4. 腾讯、阿里、京东方向不一致。",
            "5. 夜期与ADR代理分化明显。",
            "6. 开盘15分钟无法站稳开盘价。",
            "7. 成交放大但价格不涨。",
            "8. 数据源冲突，方向无法确认。",
            "",
            "## 12. 🔥今日重点观察清单",
            "1. 恒科代理：KWEB",
            "2. 港股ADR代理：TCEHY / BABA / JD",
            "3. QQQ：美股科技传导核心",
            "4. USD/CNH：港股风险阀门",
            "5. Nikkei：亚洲风险偏好",
            "6. KOSPI / KOSDAQ：亚洲成长股情绪",
            "7. ASX 200：澳洲risk-on/off",
            "8. BABA / TCEHY：核心权重代理",
            "9. XPEV / LI / NIO：高Beta情绪",
            "10. SMH：全球半导体风险偏好",
            "",
            "## 13. 🧨风险提示",
            "- 港股跳空风险",
            "- 夜期与正股偏差",
            "- ADR传导失真",
            "- USD/CNH突然上行",
            "- 日本/韩国高开低走",
            "- 美股强但中概弱",
            "- 权重股方向分化",
            "- 开盘前流动性不足",
            "- 开盘15分钟假突破",
            "- 中国政策新闻突发",
            "- 止损滑点",
            "",
            "## 14. 🧾最终交易结论",
            "今日方向：{0}".format(direction),
            "是否适合盘前建仓：{0}".format("是" if total_score >= 20 else "只适合轻仓" if total_score >= 15 else "等确认"),
            "建议仓位：{0}".format(position),
            "我的计划：",
            "- 盘前：{0}".format("轻仓试单偏多" if total_score >= 20 else "先观察外围一致性"),
            "- 开盘前30分钟：盯USD/CNH和核心ADR",
            "- 开盘后15分钟：只做站稳开盘价方向",
            "- 开盘后1–2小时：优先止盈 / 止损 / 平仓",
            "最重要的5个确认信号：",
            "1. QQQ 与 KWEB 同向",
            "2. USD/CNH 不快速上冲",
            "3. Nikkei / KOSPI 不高开低走",
            "4. 腾讯 / 阿里 / 京东代理同向",
            "5. 开盘15分钟站稳开盘价",
            "最重要的3个取消/止损条件：",
            "1. KWEB 转弱且弱于 QQQ",
            "2. USD/CNH 快速上行",
            "3. 开盘后跌破开盘价无法收复",
            "",
            "来源：",
            "- Yahoo Finance: https://finance.yahoo.com/",
            "- 仅使用可确认公开行情数据；无可靠数据项明确标注。",
        ]
    )

    return report_date, [message1, message2, message3]


def save_outputs(report_date: str, messages: List[str], reports_dir: Path) -> Tuple[Path, Path]:
    reports_dir.mkdir(parents=True, exist_ok=True)
    markdown = "\n\n".join(messages).strip() + "\n"
    md_path = reports_dir / "{0}.md".format(report_date)
    html_path = reports_dir / "{0}.html".format(report_date)
    latest_md = reports_dir / "latest.md"
    latest_html = reports_dir / "latest.html"
    md_path.write_text(markdown, encoding="utf-8")
    latest_md.write_text(markdown, encoding="utf-8")
    html_text = (
        "<!DOCTYPE html>\n<html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        "<title>{0}{1}</title>"
        "<style>body{{font-family:Arial,'PingFang SC',sans-serif;background:#f7f7f4;color:#222;line-height:1.6;padding:24px;max-width:900px;margin:0 auto}}"
        "h1,h2{{color:#164b7a}}pre{{white-space:pre-wrap}}</style></head><body><pre>{2}</pre></body></html>"
    ).format(REPORT_TITLE_PREFIX, report_date, html.escape(markdown))
    html_path.write_text(html_text, encoding="utf-8")
    latest_html.write_text(html_text, encoding="utf-8")
    return md_path, html_path


def main() -> int:
    args = parse_args()
    env = merged_env(args.env_file)
    token = env.get("TELEGRAM_BOT_TOKEN")
    chat_id = env.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        raise RuntimeError("Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID.")

    try:
        if args.schedule_guard and not should_run_for_schedule():
            print("Skipped: outside Melbourne scheduled window.")
            return 0

        if args.send_test_message:
            send_messages(
                token,
                chat_id,
                ["HK Tech Preopen Bot 测试成功\n时间：{0}".format(datetime.now(MELBOURNE_TZ).isoformat(timespec="seconds"))],
            )
            return 0

        quotes = fetch_many(QUOTE_SYMBOLS)
        enough, confirmed, reasons = data_completeness(quotes)
        report_date = datetime.now(MELBOURNE_TZ).date().isoformat()
        if enough:
            report_date, messages = build_report(quotes)
        else:
            messages = render_insufficient(report_date, confirmed, reasons)

        reports_dir = Path(args.reports_dir).expanduser()
        md_path, html_path = save_outputs(report_date, messages, reports_dir)
        state_path = Path(args.state_file).expanduser()
        state = load_state(state_path)
        digest = hashlib.sha256("\n".join(messages).encode("utf-8")).hexdigest()

        if not args.force and state.get("last_sent_report_date") == report_date:
            print("Latest report for {0} already sent.".format(report_date))
            print("Markdown saved to {0}".format(md_path))
            print("HTML saved to {0}".format(html_path))
            return 0

        send_messages(token, chat_id, messages)
        save_state(
            state_path,
            {
                "last_sent_report_date": report_date,
                "last_sent_report_id": digest,
                "last_sent_at": datetime.now(MELBOURNE_TZ).isoformat(timespec="seconds"),
                "report_markdown_path": str(md_path),
                "report_html_path": str(html_path),
            },
        )
        print("Sent report for {0}".format(report_date))
        print("Markdown saved to {0}".format(md_path))
        print("HTML saved to {0}".format(html_path))
        return 0
    except Exception as exc:
        send_failure_alert(token, chat_id, repr(exc))
        raise


if __name__ == "__main__":
    raise SystemExit(main())
