"""披露时间 → 首个可交易日。

规则（A 股，Asia/Shanghai）：
- 交易日 15:00 前披露：当日即可反映（盘前披露于开盘反映，盘中披露于盘中反映）
- 交易日 15:00 及之后、或非交易日披露：顺延到下一个交易日
交易日历来自扶摇 /api/a-share/calendar/trading-days（仅覆盖近一年）。
日历不可用或日期超出覆盖范围时，退化为"按工作日估算"，并显式标注，不静默当作准确结果。
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Optional

from app.config import CN_TZ

MARKET_CLOSE = time(15, 0)


def parse_trading_days(data: Optional[dict]) -> list[date]:
    if not data:
        return []
    out = []
    for it in data.get("item", []) or []:
        d = it.get("date")
        if isinstance(d, str) and len(d) == 8:
            out.append(date(int(d[:4]), int(d[4:6]), int(d[6:])))
    return sorted(out)


def effective_trade_date(dt: datetime, trading_days: list[date]) -> dict:
    local = dt.astimezone(CN_TZ)
    d, t = local.date(), local.time()
    covered = bool(trading_days) and trading_days[0] <= d <= trading_days[-1]
    if covered:
        days = set(trading_days)
        if d in days and t < MARKET_CLOSE:
            return {"date": d.isoformat(), "method": "calendar",
                    "note": "交易时段或盘前披露，当日可反映" if t >= time(9, 30) else "盘前披露，当日开盘可反映"}
        nxt = next((x for x in trading_days if x > d), None)
        if nxt:
            return {"date": nxt.isoformat(), "method": "calendar",
                    "note": "盘后或非交易日披露，顺延至下一交易日"}
    # 退化：只排除周末，节假日无法识别
    cur = d
    if cur.weekday() < 5 and t < MARKET_CLOSE:
        res = cur
    else:
        res = cur + timedelta(days=1)
        while res.weekday() >= 5:
            res += timedelta(days=1)
    reason = "交易日历不可用" if not trading_days else "日期超出交易日历覆盖范围"
    return {"date": res.isoformat(), "method": "weekday_estimate",
            "note": f"{reason}，按工作日估算，未剔除法定节假日"}
