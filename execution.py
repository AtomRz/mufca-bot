import asyncio
import logging
from typing import Dict, Optional, Tuple
import ccxt

logger = logging.getLogger(__name__)

class GateExecutor:
    def __init__(self, exchange: ccxt.Exchange):
        self.exchange = exchange
        self._live_positions: Dict[str, Dict] = {}

    async def fetch_balance_usdt(self) -> float:
        try:
            bal = await asyncio.to_thread(self.exchange.fetch_balance)
            return float(bal.get("USDT", {}).get("free", 0))
        except Exception as e:
            logger.error(f"Balance fetch error: {e}")
            return 0.0

    async def get_market_limits(self, symbol: str) -> Dict:
        try:
            markets = await asyncio.to_thread(self.exchange.load_markets)
            m = markets.get(symbol, {})
            return {
                "min_amount": m.get("limits", {}).get("amount", {}).get("min", 0) or 0,
                "min_cost":   m.get("limits", {}).get("cost", {}).get("min", 0) or 0,
                "precision":  m.get("precision", {}).get("amount", 8),
                "contract":   m.get("contract", False),
            }
        except Exception as e:
            logger.error(f"Market info error: {e}")
            return {"min_amount": 0, "min_cost": 0, "precision": 8, "contract": False}

    async def set_leverage(self, symbol: str, lev: int) -> bool:
        if self.exchange.options.get("defaultType") != "swap":
            return True
        try:
            await asyncio.to_thread(self.exchange.set_leverage, lev, symbol)
            return True
        except Exception as e:
            logger.warning(f"Set leverage failed: {e}")
            return False

    async def open_position(
        self,
        symbol: str,
        side: str,
        amount: float,
        leverage: int = 1,
        tp_price: Optional[float] = None,
        sl_price: Optional[float] = None,
        order_type: str = "market",
        limit_price: Optional[float] = None,
        use_cost: bool = False,
    ) -> Tuple[bool, str, Optional[Dict]]:
        try:
            balance = await self.fetch_balance_usdt()
            if balance <= 0:
                return False, "❌ Нулевой баланс USDT", None

            limits = await self.get_market_limits(symbol)
            if use_cost and limits["min_cost"] and amount < limits["min_cost"]:
                return False, f"❌ Минимум {limits['min_cost']} USDT", None

            if leverage > 1:
                await self.set_leverage(symbol, leverage)

            params = {}
            if use_cost:
                params["cost"] = amount

            if tp_price:
                params["takeProfit"] = {"triggerPrice": tp_price}
            if sl_price:
                params["stopLoss"] = {"triggerPrice": sl_price}

            price = limit_price if order_type == "limit" else None

            order = await asyncio.to_thread(
                self.exchange.create_order,
                symbol, order_type, side, amount, price, params
            )

            self._live_positions[symbol] = {
                "side": side,
                "amount": amount,
                "entry": float(order.get("average", limit_price or 0)),
                "leverage": leverage,
                "tp": tp_price,
                "sl": sl_price,
                "order_id": order.get("id"),
                "use_cost": use_cost,
            }

            return True, "✅ Ордер размещен", order

        except ccxt.InsufficientFunds:
            return False, "❌ Недостаточно средств", None
        except ccxt.InvalidOrder as e:
            return False, f"❌ Неверные параметры: {e}", None
        except Exception as e:
            logger.exception(f"Execution error: {e}")
            return False, f"❌ Ошибка: {str(e)[:200]}", None

    def get_positions(self) -> Dict[str, Dict]:
        return self._live_positions.copy()

    async def close_position(self, symbol: str) -> Tuple[bool, str]:
        pos = self._live_positions.get(symbol)
        if not pos:
            return False, "Нет открытой позиции в памяти бота"
        
        close_side = "sell" if pos["side"] == "buy" else "buy"
        try:
            params = {"reduceOnly": True}
            if pos["use_cost"]:
                params["cost"] = pos["amount"]
                amount = None
            else:
                amount = pos["amount"]

            await asyncio.to_thread(
                self.exchange.create_order,
                symbol, "market", close_side, amount, None, params
            )
            del self._live_positions[symbol]
            return True, f"✅ Позиция {symbol} закрыта"
        except Exception as e:
            return False, f"❌ Ошибка закрытия: {e}"