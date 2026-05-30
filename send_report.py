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
