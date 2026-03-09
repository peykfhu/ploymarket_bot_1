"""
Polymarket Trading Bot - 主服务
所有逻辑集中在一个文件，避免导入顺序问题
"""

import os
import asyncio
import logging
import json
import time
import math
import re
import hmac
import hashlib
from datetime import datetime, date
from typing import List, Optional, Dict
from contextlib import asynccontextmanager

import httpx
import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import create_engine, Column, Integer, Float, String, DateTime, Boolean
from sqlalchemy.orm import declarative_base, sessionmaker
from pydantic import BaseModel

# ============================================================
#  配置
# ============================================================
load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S"
)
logger = logging.getLogger("bot")


class Config:
    POLYMARKET_API_KEY = os.getenv("POLYMARKET_API_KEY", "")
    POLYMARKET_SECRET = os.getenv("POLYMARKET_SECRET", "")
    POLYMARKET_WALLET = os.getenv("POLYMARKET_WALLET_ADDRESS", "")
    POLYMARKET_PRIVATE_KEY = os.getenv("POLYMARKET_PRIVATE_KEY", "")
    POLYMARKET_API_URL = os.getenv("POLYMARKET_API_URL", "https://clob.polymarket.com")
    NOAA_TOKEN = os.getenv("NOAA_API_TOKEN", "")
    BINANCE_API_KEY = os.getenv("BINANCE_API_KEY", "")
    BINANCE_SECRET = os.getenv("BINANCE_SECRET", "")
    TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
    TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
    MAX_POSITION = float(os.getenv("MAX_POSITION_SIZE", "50"))
    MAX_DAILY_LOSS = float(os.getenv("MAX_DAILY_LOSS", "200"))
    INITIAL_BALANCE = float(os.getenv("INITIAL_BALANCE", "150"))
    DRY_RUN = os.getenv("DRY_RUN", "true").lower() == "true"


CFG = Config()

# ============================================================
#  数据库 - 在最前面初始化，绝对不会有顺序问题
# ============================================================
DB_PATH = "/app/data/trades.db"
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

Base = declarative_base()


class TradeRecord(Base):
    __tablename__ = "trades"
    id = Column(Integer, primary_key=True, autoincrement=True)
    agent = Column(String, index=True)
    market_id = Column(String)
    market_title = Column(String)
    side = Column(String)
    price = Column(Float)
    quantity = Column(Float)
    cost = Column(Float)
    pnl = Column(Float, default=0.0)
    status = Column(String, default="open")
    edge = Column(Float)
    signal_source = Column(String)
    signal_prob = Column(Float)
    market_prob = Column(Float)
    dry_run = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    closed_at = Column(DateTime, nullable=True)


class AgentStateRecord(Base):
    __tablename__ = "agent_states"
    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String, unique=True, index=True)
    status = Column(String, default="idle")
    trades = Column(Integer, default=0)
    wins = Column(Integer, default=0)
    pnl = Column(Float, default=0.0)
    last_signal = Column(String, default="")
    last_active = Column(DateTime, default=datetime.utcnow)
    errors = Column(Integer, default=0)
    last_error = Column(String, default="")


engine = create_engine(f"sqlite:///{DB_PATH}", echo=False)
Session = sessionmaker(bind=engine)

# 建表 - 立即执行
Base.metadata.create_all(engine)
logger.info(f"数据库就绪: {DB_PATH}")


def db():
    return Session()


# ============================================================
#  Polymarket 客户端
# ============================================================
class PolyClient:
    def __init__(self):
        self.base = CFG.POLYMARKET_API_URL
        self.http = httpx.AsyncClient(timeout=20.0)

    async def get_markets(self, query="", limit=30):
        try:
            r = await self.http.get(f"{self.base}/markets", params={"query": query, "limit": limit})
            if r.status_code == 200:
                data = r.json()
                # API可能返回列表或带data字段的对象
                if isinstance(data, list):
                    return data
                elif isinstance(data, dict):
                    return data.get("data", data.get("markets", []))
            return []
        except Exception as e:
            logger.warning(f"获取市场失败: {e}")
            return []

    async def search(self, keywords: List[str], limit=15):
        """搜索多个关键词的市场"""
        all_markets = []
        seen = set()
        for kw in keywords:
            markets = await self.get_markets(query=kw, limit=limit)
            for m in markets:
                mid = m.get("condition_id", m.get("id", ""))
                if mid and mid not in seen:
                    seen.add(mid)
                    all_markets.append(m)
            await asyncio.sleep(0.3)  # 避免频率限制
        return all_markets

    async def get_price(self, market):
        """从市场数据中获取YES价格"""
        tokens = market.get("tokens", [])
        if tokens:
            return float(tokens[0].get("price", 0.5))
        # 备用
        return float(market.get("outcomePrices", [0.5, 0.5])[0])

    async def place_order(self, token_id, side, price, size):
        if CFG.DRY_RUN:
            logger.info(f"[模拟] {side} {size:.1f}@{price:.3f} | {token_id[:20]}...")
            return {"id": f"sim_{int(time.time()*1000)}", "status": "simulated"}

        try:
            body = json.dumps({
                "tokenID": token_id,
                "side": side, "price": str(price),
                "size": str(size), "type": "GTC"
            })
            ts = str(int(time.time()))
            sig = hmac.new(
                CFG.POLYMARKET_SECRET.encode(),
                f"{ts}POST/order{body}".encode(),
                hashlib.sha256
            ).hexdigest()
            headers = {
                "POLY_API_KEY": CFG.POLYMARKET_API_KEY,
                "POLY_SIGNATURE": sig,
                "POLY_TIMESTAMP": ts,
                "Content-Type": "application/json",
            }
            r = await self.http.post(f"{self.base}/order", content=body, headers=headers)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            logger.error(f"下单失败: {e}")
            return None

    async def close(self):
        await self.http.aclose()


# ============================================================
#  风控
# ============================================================
class RiskControl:
    MIN_EDGE = 0.08         # 8% 最小优势
    MAX_BET = 25.0          # 单笔最大
    MAX_OPEN = 20           # 最大持仓数

    @staticmethod
    def check(edge, price, market_id="", agent=""):
        if edge < RiskControl.MIN_EDGE:
            return False, f"优势不足{edge:.0%}"
        if price <= 0.02 or price >= 0.98:
            return False, "价格极端"

        s = db()
        try:
            # 日亏损
            today = date.today().isoformat()
            day_pnl = sum(t.pnl for t in s.query(TradeRecord).filter(
                TradeRecord.created_at >= today, TradeRecord.status == "closed"
            ).all())
            if day_pnl < -CFG.MAX_DAILY_LOSS:
                return False, f"日亏损${day_pnl:.0f}"

            # 持仓数
            opens = s.query(TradeRecord).filter(TradeRecord.status == "open").count()
            if opens >= RiskControl.MAX_OPEN:
                return False, f"持仓满{opens}"

            # 重复
            dup = s.query(TradeRecord).filter(
                TradeRecord.market_id == market_id,
                TradeRecord.agent == agent,
                TradeRecord.status == "open"
            ).first()
            if dup:
                return False, "重复持仓"
        finally:
            s.close()

        return True, "OK"

    @staticmethod
    def calc_size(signal_prob, price):
        """Kelly准则计算仓位"""
        if price <= 0:
            return 0
        b = (1.0 / price) - 1
        if b <= 0:
            return 0
        kelly = (signal_prob * b - (1 - signal_prob)) / b
        frac = max(0, kelly * 0.25)  # 1/4 Kelly

        s = db()
        try:
            total_pnl = sum(t.pnl for t in s.query(TradeRecord).filter(
                TradeRecord.status == "closed").all())
            open_cost = sum(t.cost for t in s.query(TradeRecord).filter(
                TradeRecord.status == "open").all())
            balance = CFG.INITIAL_BALANCE + total_pnl - open_cost
        finally:
            s.close()

        bet = min(balance * frac, RiskControl.MAX_BET, CFG.MAX_POSITION)
        qty = bet / price if price > 0 else 0
        return round(max(0, qty), 2)


# ============================================================
#  全局事件系统
# ============================================================
event_log: List[dict] = []
ws_clients: List[WebSocket] = []
START_TIME = datetime.utcnow()


async def emit(agent: str, msg: str, typ: str = "info"):
    """发送事件到前端"""
    evt = {
        "time": datetime.utcnow().strftime("%H:%M:%S"),
        "agent": agent,
        "msg": msg,
        "type": typ,
    }
    event_log.append(evt)
    if len(event_log) > 500:
        event_log.pop(0)

    for c in ws_clients[:]:
        try:
            await c.send_json({"type": "event", "data": evt})
        except:
            if c in ws_clients:
                ws_clients.remove(c)


async def record_trade(agent, market_id, title, side, price, qty, edge,
                       signal_source, signal_prob, market_prob):
    """记录交易"""
    s = db()
    try:
        t = TradeRecord(
            agent=agent, market_id=market_id, market_title=title,
            side=side, price=price, quantity=qty, cost=price * qty,
            edge=edge, signal_source=signal_source,
            signal_prob=signal_prob, market_prob=market_prob,
            dry_run=CFG.DRY_RUN,
        )
        s.add(t)

        # 更新 agent 统计
        state = s.query(AgentStateRecord).filter(AgentStateRecord.name == agent).first()
        if state:
            state.trades += 1
            state.last_active = datetime.utcnow()
            state.last_signal = f"{side}@{price:.2f} {title[:30]}"
        s.commit()
    finally:
        s.close()


# ============================================================
#  Agent 引擎
# ============================================================
class AgentEngine:
    """统一的Agent调度引擎"""

    def __init__(self, poly: PolyClient):
        self.poly = poly
        self.running = False
        self.http = httpx.AsyncClient(timeout=20.0)
        self.known_injuries = set()

        # Agent 配置
        self.agents_config = {
            "weather": {
                "name": "🌧️ 气象狙击手",
                "interval": 600,
                "scan": self.scan_weather,
            },
            "crypto": {
                "name": "₿ 加密猎人",
                "interval": 300,
                "scan": self.scan_crypto,
            },
            "politics": {
                "name": "🏛️ 政治收割机",
                "interval": 1800,
                "scan": self.scan_politics,
            },
            "sports": {
                "name": "🏀 伤病监控",
                "interval": 600,
                "scan": self.scan_sports,
            },
        }

    async def start(self):
        self.running = True

        # 初始化 agent states
        s = db()
        try:
            for key in self.agents_config:
                if not s.query(AgentStateRecord).filter(AgentStateRecord.name == key).first():
                    s.add(AgentStateRecord(name=key, status="running"))
            s.commit()
        finally:
            s.close()

        # 启动每个agent的循环
        for key, cfg in self.agents_config.items():
            asyncio.create_task(self._agent_loop(key, cfg))
            await emit(key, f"{cfg['name']} 已启动", "info")

    async def stop(self):
        self.running = False
        await self.http.aclose()

    async def _agent_loop(self, key, cfg):
        """单个Agent的运行循环"""
        while self.running:
            try:
                self._set_status(key, "running")
                signals = await cfg["scan"]()

                for sig in signals:
                    ok, reason = RiskControl.check(
                        sig["edge"], sig["price"], sig.get("market_id", ""), key
                    )
                    if ok:
                        qty = RiskControl.calc_size(sig["signal_prob"], sig["price"])
                        if qty > 0:
                            result = await self.poly.place_order(
                                sig["market_id"], "BUY", sig["price"], qty
                            )
                            if result:
                                await record_trade(
                                    key, sig["market_id"], sig["title"],
                                    sig["side"], sig["price"], qty, sig["edge"],
                                    sig["source"], sig["signal_prob"], sig["market_prob"]
                                )
                                cost = sig["price"] * qty
                                await emit(key,
                                    f"{'📈' if sig['side']=='YES' else '📉'} "
                                    f"{sig['side']} @{sig['price']:.2f} "
                                    f"| {sig['title'][:35]}... "
                                    f"| 优势{sig['edge']:.0%} 花费${cost:.2f}",
                                    "trade"
                                )
                    else:
                        logger.debug(f"[{key}] 拒绝: {reason}")

                self._update_active(key)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[{key}] 错误: {e}")
                self._record_error(key, str(e))
                await emit(key, f"错误: {str(e)[:80]}", "error")

            await asyncio.sleep(cfg["interval"])

    # ========== Agent-01: 气象狙击手 ==========
    CITIES = {
        "Chicago":      {"office": "LOT", "x": 65, "y": 76},
        "New York":     {"office": "OKX", "x": 33, "y": 37},
        "Los Angeles":  {"office": "LOX", "x": 154, "y": 44},
        "Miami":        {"office": "MFL", "x": 110, "y": 50},
        "Houston":      {"office": "HGX", "x": 65, "y": 97},
        "Seattle":      {"office": "SEW", "x": 124, "y": 67},
        "Denver":       {"office": "BOU", "x": 62, "y": 60},
        "Atlanta":      {"office": "FFC", "x": 50, "y": 86},
        "Phoenix":      {"office": "PSR", "x": 159, "y": 58},
        "Dallas":       {"office": "FWD", "x": 80, "y": 103},
    }

    async def scan_weather(self):
        signals = []
        await emit("weather", "扫描NOAA气象数据...", "info")

        # 获取预报
        forecasts = {}
        for city, info in self.CITIES.items():
            try:
                url = f"https://api.weather.gov/gridpoints/{info['office']}/{info['x']},{info['y']}/forecast"
                r = await self.http.get(url, headers={
                    "User-Agent": "(PolyBot, bot@example.com)",
                    "Accept": "application/geo+json",
                })
                if r.status_code == 200:
                    periods = r.json().get("properties", {}).get("periods", [])
                    if periods:
                        p = periods[0]
                        precip = p.get("probabilityOfPrecipitation", {})
                        rain = (precip.get("value") or 0) / 100.0
                        forecasts[city] = {
                            "rain": rain,
                            "temp": p.get("temperature", 0),
                            "desc": p.get("shortForecast", ""),
                        }
                await asyncio.sleep(0.4)
            except Exception as e:
                logger.debug(f"NOAA {city}: {e}")

        if not forecasts:
            await emit("weather", "NOAA无数据", "info")
            return signals

        # 获取天气市场
        markets = await self.poly.search(
            ["rain", "weather", "temperature", "snow", "storm"], limit=10
        )

        found = 0
        for m in markets:
            q = (m.get("question") or m.get("title") or "").lower()
            mid = m.get("condition_id", m.get("id", ""))
            mkt_price = await self.poly.get_price(m)

            for city, fc in forecasts.items():
                if city.lower() not in q:
                    continue

                noaa_prob = None
                if any(w in q for w in ["rain", "precip", "shower", "storm"]):
                    noaa_prob = fc["rain"]
                if noaa_prob is None:
                    continue

                edge = noaa_prob - mkt_price
                if edge >= 0.10:
                    signals.append({
                        "market_id": mid,
                        "title": m.get("question", m.get("title", q)),
                        "side": "YES", "price": mkt_price,
                        "edge": edge, "source": f"NOAA-{city}",
                        "signal_prob": noaa_prob, "market_prob": mkt_price,
                    })
                    found += 1
                    await emit("weather",
                        f"🎯 {city}: NOAA={noaa_prob:.0%} 市场={mkt_price:.0%} 优势={edge:.0%}",
                        "signal"
                    )
                elif edge <= -0.10:
                    signals.append({
                        "market_id": mid,
                        "title": m.get("question", m.get("title", q)),
                        "side": "NO", "price": 1.0 - mkt_price,
                        "edge": abs(edge), "source": f"NOAA-{city}",
                        "signal_prob": 1 - noaa_prob, "market_prob": 1 - mkt_price,
                    })
                    found += 1

        await emit("weather", f"扫描完成: {len(forecasts)}城市, {found}个信号", "info")
        return signals

    # ========== Agent-02: 加密差价猎人 ==========
    async def scan_crypto(self):
        signals = []
        await emit("crypto", "获取BTC价格...", "info")

        # 获取币安价格
        btc = None
        try:
            r = await self.http.get("https://api.binance.com/api/v3/ticker/price",
                                     params={"symbol": "BTCUSDT"})
            if r.status_code == 200:
                btc = float(r.json()["price"])
        except:
            pass

        if not btc:
            try:
                r = await self.http.get("https://api.coingecko.com/api/v3/simple/price",
                                         params={"ids": "bitcoin", "vs_currencies": "usd"})
                btc = float(r.json()["bitcoin"]["usd"])
            except:
                await emit("crypto", "无法获取BTC价格", "error")
                return signals

        await emit("crypto", f"BTC=${btc:,.0f}", "info")

        # 搜索加密市场
        markets = await self.poly.search(
            ["bitcoin", "BTC", "crypto", "ethereum"], limit=10
        )

        for m in markets:
            q = (m.get("question") or m.get("title") or "")
            mid = m.get("condition_id", m.get("id", ""))
            mkt_price = await self.poly.get_price(m)

            target = self._extract_btc_target(q)
            if not target:
                continue

            est_prob = self._btc_probability(btc, target, q)
            if est_prob is None:
                continue

            edge = est_prob - mkt_price

            if abs(edge) >= 0.08:
                side = "YES" if edge > 0 else "NO"
                price = mkt_price if side == "YES" else 1 - mkt_price
                signals.append({
                    "market_id": mid, "title": q,
                    "side": side, "price": price,
                    "edge": abs(edge), "source": f"Binance BTC=${btc:,.0f}",
                    "signal_prob": est_prob if side == "YES" else 1 - est_prob,
                    "market_prob": mkt_price if side == "YES" else 1 - mkt_price,
                })
                await emit("crypto",
                    f"🎯 目标${target:,} 估算={est_prob:.0%} 市场={mkt_price:.0%}",
                    "signal"
                )

        return signals

    def _extract_btc_target(self, q):
        for p in [r'\$([0-9]{1,3}(?:,?[0-9]{3})*)', r'(\d{4,6})\s*(?:usd|dollar)',
                  r'(?:above|below|reach|hit)\s+\$?(\d{1,3}(?:,?\d{3})*)']:
            m = re.search(p, q, re.I)
            if m:
                try:
                    v = float(m.group(1).replace(",", ""))
                    if 1000 < v < 10000000:
                        return v
                except:
                    pass
        return None

    def _btc_probability(self, current, target, q):
        ql = q.lower()
        is_above = any(w in ql for w in ["above", "reach", "over", "exceed", "hit"])
        dist = (target - current) / current
        vol = 0.04 * math.sqrt(7)  # 7天波动
        if vol == 0:
            return None
        z = dist / vol
        prob = 0.5 * (1 - math.erf(z / math.sqrt(2)))
        if not is_above:
            prob = 1 - prob
        return max(0.01, min(0.99, prob))

    # ========== Agent-03: 政治收割机 ==========
    async def scan_politics(self):
        signals = []
        await emit("politics", "扫描政治合约...", "info")

        markets = await self.poly.search(
            ["election", "president", "trump", "biden", "harris",
             "congress", "senate", "governor", "vote"], limit=20
        )

        if not markets:
            await emit("politics", "未找到政治合约", "info")
            return signals

        # 获取民调数据
        poll_data = await self._fetch_polls()

        for m in markets:
            q = (m.get("question") or m.get("title") or "")
            mid = m.get("condition_id", m.get("id", ""))
            mkt_price = await self.poly.get_price(m)

            poll_est = self._match_polls(q.lower(), poll_data)
            if poll_est is None:
                continue

            edge = poll_est - mkt_price
            if abs(edge) >= 0.08:
                side = "YES" if edge > 0 else "NO"
                price = mkt_price if side == "YES" else 1 - mkt_price
                signals.append({
                    "market_id": mid, "title": q,
                    "side": side, "price": price,
                    "edge": abs(edge), "source": "民调数据",
                    "signal_prob": poll_est if side == "YES" else 1 - poll_est,
                    "market_prob": mkt_price if side == "YES" else 1 - mkt_price,
                })
                await emit("politics",
                    f"🎯 {q[:40]}... 民调={poll_est:.0%} 市场={mkt_price:.0%}",
                    "signal"
                )

        return signals

    async def _fetch_polls(self):
        polls = {}
        try:
            r = await self.http.get(
                "https://projects.fivethirtyeight.com/polls/president-general/2024/national/polls.json",
                headers={"User-Agent": "Mozilla/5.0"}, timeout=10
            )
            if r.status_code == 200:
                polls["538"] = r.json()
        except:
            pass
        return polls

    def _match_polls(self, question, poll_data):
        names = ["trump", "biden", "harris", "desantis", "haley",
                 "kennedy", "rfk", "newsom"]
        found = [n for n in names if n in question]
        if not found:
            return None

        for src, data in poll_data.items():
            if isinstance(data, list):
                for poll in data[-5:]:  # 最近5个
                    answers = poll.get("answers", [])
                    for a in answers:
                        name = a.get("choice", "").lower()
                        if any(f in name for f in found):
                            pct = a.get("pct", 0)
                            return pct / 100.0 if pct > 1 else pct
        return None

    # ========== Agent-04: 体育伤病 ==========
    async def scan_sports(self):
        signals = []
        await emit("sports", "扫描伤病报告...", "info")

        injuries = []
        for sport_url in [
            "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/injuries",
            "https://site.api.espn.com/apis/site/v2/sports/football/nfl/injuries",
        ]:
            try:
                r = await self.http.get(sport_url, headers={"User-Agent": "Mozilla/5.0"})
                if r.status_code == 200:
                    data = r.json()
                    for team in data.get("teams", data.get("items", [])):
                        if not isinstance(team, dict):
                            continue
                        team_name = team.get("team", {}).get("displayName", "")
                        for inj in team.get("injuries", []):
                            player = inj.get("athlete", {}).get("displayName", "")
                            status = inj.get("status", inj.get("type", {}).get("description", ""))
                            key = f"{player}_{status}"
                            if key not in self.known_injuries:
                                self.known_injuries.add(key)
                                injuries.append({
                                    "player": player, "team": team_name,
                                    "status": status.lower()
                                })
            except Exception as e:
                logger.debug(f"ESPN: {e}")

        if not injuries:
            await emit("sports", "无新伤病", "info")
            return signals

        await emit("sports", f"发现{len(injuries)}条伤病", "info")

        markets = await self.poly.search(
            ["NBA", "NFL", "basketball", "football", "soccer", "MLB"], limit=15
        )

        impact_map = {
            "out": -0.15, "doubtful": -0.10, "questionable": -0.05,
            "day-to-day": -0.08, "injured reserve": -0.18, "suspended": -0.12,
        }

        for inj in injuries:
            for m in markets:
                q = (m.get("question") or m.get("title") or "").lower()
                if inj["team"].lower() not in q:
                    continue

                mid = m.get("condition_id", m.get("id", ""))
                mkt_price = await self.poly.get_price(m)

                impact = 0
                for k, v in impact_map.items():
                    if k in inj["status"]:
                        impact = v
                        break
                if abs(impact) < 0.05:
                    continue

                adj_prob = max(0.02, min(0.98, mkt_price + impact))
                edge = abs(impact)

                if edge >= 0.08:
                    side = "NO" if impact < 0 else "YES"
                    price = (1 - mkt_price) if side == "NO" else mkt_price
                    signals.append({
                        "market_id": mid, "title": m.get("question", m.get("title", "")),
                        "side": side, "price": price,
                        "edge": edge,
                        "source": f"伤病: {inj['player']}({inj['status']})",
                        "signal_prob": adj_prob if side == "YES" else 1 - adj_prob,
                        "market_prob": mkt_price if side == "YES" else 1 - mkt_price,
                    })
                    await emit("sports",
                        f"🎯 {inj['player']}({inj['team']}) {inj['status']} 影响{impact:+.0%}",
                        "signal"
                    )

        return signals

    # ===== 工具方法 =====
    def _set_status(self, name, status):
        s = db()
        try:
            st = s.query(AgentStateRecord).filter(AgentStateRecord.name == name).first()
            if st:
                st.status = status
                s.commit()
        finally:
            s.close()

    def _update_active(self, name):
        s = db()
        try:
            st = s.query(AgentStateRecord).filter(AgentStateRecord.name == name).first()
            if st:
                st.last_active = datetime.utcnow()
                s.commit()
        finally:
            s.close()

    def _record_error(self, name, err):
        s = db()
        try:
            st = s.query(AgentStateRecord).filter(AgentStateRecord.name == name).first()
            if st:
                st.errors += 1
                st.last_error = err[:300]
                st.status = "error"
                s.commit()
        finally:
            s.close()


# ============================================================
#  FastAPI 应用
# ============================================================
engine_instance: Optional[AgentEngine] = None
poly_client: Optional[PolyClient] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global engine_instance, poly_client

    logger.info("=" * 50)
    logger.info("🤖 Polymarket Trading Bot 启动")
    logger.info(f"💰 初始资金: ${CFG.INITIAL_BALANCE}")
    logger.info(f"🧪 模式: {'模拟' if CFG.DRY_RUN else '⚠️ 实盘'}")
    logger.info("=" * 50)

    poly_client = PolyClient()
    engine_instance = AgentEngine(poly_client)
    await engine_instance.start()

    # 面板推送
    push_task = asyncio.create_task(push_loop())

    logger.info("🚀 启动完成!")
    yield

    push_task.cancel()
    await engine_instance.stop()
    await poly_client.close()
    logger.info("已关闭")


app = FastAPI(title="Polymarket Bot", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_credentials=True,
    allow_methods=["*"], allow_headers=["*"],
)


@app.get("/api/health")
def health():
    return {"status": "ok", "uptime": (datetime.utcnow() - START_TIME).total_seconds()}


@app.get("/api/dashboard")
def dashboard():
    return build_dashboard()


@app.get("/api/trades")
def get_trades(limit: int = 50):
    s = db()
    try:
        trades = s.query(TradeRecord).order_by(TradeRecord.created_at.desc()).limit(limit).all()
        return [{
            "id": t.id, "agent": t.agent, "market": t.market_title,
            "side": t.side, "price": t.price, "qty": t.quantity,
            "cost": t.cost, "pnl": t.pnl, "status": t.status,
            "edge": t.edge, "source": t.signal_source,
            "dry_run": t.dry_run,
            "time": t.created_at.isoformat() if t.created_at else "",
        } for t in trades]
    finally:
        s.close()


@app.get("/api/events")
def get_events(limit: int = 100):
    return event_log[-limit:]


@app.get("/api/config")
def get_cfg():
    return {
        "dry_run": CFG.DRY_RUN,
        "initial_balance": CFG.INITIAL_BALANCE,
        "polymarket": bool(CFG.POLYMARKET_API_KEY),
        "noaa": bool(CFG.NOAA_TOKEN),
        "binance": bool(CFG.BINANCE_API_KEY),
    }


@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket):
    await websocket.accept()
    ws_clients.append(websocket)
    try:
        await websocket.send_json({"type": "init", "data": build_dashboard()})
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        if websocket in ws_clients:
            ws_clients.remove(websocket)


async def push_loop():
    while True:
        await asyncio.sleep(3)
        if ws_clients:
            data = build_dashboard()
            for c in ws_clients[:]:
                try:
                    await c.send_json({"type": "dashboard", "data": data})
                except:
                    if c in ws_clients:
                        ws_clients.remove(c)


def build_dashboard():
    s = db()
    try:
        all_t = s.query(TradeRecord).all()
        closed = [t for t in all_t if t.status == "closed"]
        opens = [t for t in all_t if t.status == "open"]
        total_pnl = sum(t.pnl for t in closed)
        wins = [t for t in closed if t.pnl > 0]
        wr = len(wins) / len(closed) * 100 if closed else 0
        bal = CFG.INITIAL_BALANCE + total_pnl
        uptime = (datetime.utcnow() - START_TIME).total_seconds() / 3600

        agents_data = []
        for st in s.query(AgentStateRecord).all():
            awr = (st.wins / st.trades * 100) if st.trades > 0 else 0
            agents_data.append({
                "name": st.name, "status": st.status,
                "trades": st.trades, "wins": st.wins,
                "win_rate": round(awr, 1), "pnl": round(st.pnl, 2),
                "last_signal": st.last_signal or "",
                "last_active": st.last_active.isoformat() if st.last_active else "",
                "errors": st.errors,
            })

        recent = sorted(all_t, key=lambda x: x.created_at or datetime.min, reverse=True)[:15]

        return {
            "balance": round(bal, 2),
            "initial": CFG.INITIAL_BALANCE,
            "pnl": round(total_pnl, 2),
            "total_trades": len(all_t),
            "wins": len(wins),
            "win_rate": round(wr, 1),
            "open_positions": len(opens),
            "agents": agents_data,
            "trades": [{
                "id": t.id, "agent": t.agent,
                "market": (t.market_title or "")[:45],
                "side": t.side, "price": t.price,
                "pnl": t.pnl, "status": t.status,
                "edge": t.edge,
                "time": t.created_at.strftime("%H:%M:%S") if t.created_at else "",
            } for t in recent],
            "uptime": round(uptime, 1),
            "dry_run": CFG.DRY_RUN,
            "events": event_log[-8:],
        }
    except Exception as e:
        logger.error(f"Dashboard error: {e}")
        return {"balance": 0, "initial": 0, "pnl": 0, "total_trades": 0,
                "wins": 0, "win_rate": 0, "open_positions": 0,
                "agents": [], "trades": [], "uptime": 0,
                "dry_run": True, "events": []}
    finally:
        s.close()


# ============================================================
#  启动
# ============================================================
if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")