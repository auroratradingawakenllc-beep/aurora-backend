from flask import Flask, jsonify, request
from flask_cors import CORS
import sqlite3
import requests
import json
import os
from datetime import datetime

app = Flask(__name__)
CORS(app)

DB_FILE = "aurora.db"
STRATEGY_STATE_FILE = "strategy_state.json"

STARTING_CASH = 10000.00
DEFAULT_SYMBOL = "BTC/USD"
REQUEST_TIMEOUT = 10


# =========================
# GENERAL HELPERS
# =========================
def safe_float(value, default=0.0):
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def now_iso():
    return datetime.now().isoformat()


def file_exists(path):
    return os.path.exists(path) and os.path.isfile(path)


# =========================
# DATABASE HELPERS
# =========================
def get_connection():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_connection()
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS paper_trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            side TEXT NOT NULL,
            symbol TEXT NOT NULL DEFAULT 'BTC/USD',
            price REAL NOT NULL DEFAULT 0,
            usd_amount REAL NOT NULL DEFAULT 0,
            asset_quantity REAL NOT NULL DEFAULT 0,
            strategy_reason TEXT,
            created_at TEXT NOT NULL
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS portfolio_stats (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            btc_price REAL NOT NULL DEFAULT 0,
            btc_quantity REAL NOT NULL DEFAULT 0,
            btc_value REAL NOT NULL DEFAULT 0,
            cash_balance REAL NOT NULL DEFAULT 10000,
            total_value REAL NOT NULL DEFAULT 10000,
            updated_at TEXT NOT NULL
        )
    """)

    conn.commit()

    cur.execute("SELECT COUNT(*) AS count FROM portfolio_stats")
    row = cur.fetchone()
    count = row["count"] if row else 0

    if count == 0:
        cur.execute("""
            INSERT INTO portfolio_stats (
                btc_price, btc_quantity, btc_value, cash_balance, total_value, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?)
        """, (
            0.0,
            0.0,
            0.0,
            STARTING_CASH,
            STARTING_CASH,
            now_iso()
        ))
        conn.commit()

    conn.close()


def get_latest_portfolio_row():
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT *
        FROM portfolio_stats
        ORDER BY id DESC
        LIMIT 1
    """)
    row = cur.fetchone()
    conn.close()
    return row


def insert_portfolio_snapshot(btc_price, btc_quantity, btc_value, cash_balance, total_value):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO portfolio_stats (
            btc_price, btc_quantity, btc_value, cash_balance, total_value, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?)
    """, (
        safe_float(btc_price),
        safe_float(btc_quantity),
        safe_float(btc_value),
        safe_float(cash_balance),
        safe_float(total_value),
        now_iso()
    ))
    conn.commit()
    conn.close()


def get_trade_history(limit=100):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT id, side, symbol, price, usd_amount, asset_quantity, strategy_reason, created_at
        FROM paper_trades
        ORDER BY id DESC
        LIMIT ?
    """, (limit,))
    rows = cur.fetchall()
    conn.close()
    return rows


def insert_trade(side, symbol, price, usd_amount, asset_quantity, strategy_reason="manual/api"):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO paper_trades (
            side, symbol, price, usd_amount, asset_quantity, strategy_reason, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (
        side.lower(),
        symbol,
        safe_float(price),
        safe_float(usd_amount),
        safe_float(asset_quantity),
        strategy_reason,
        now_iso()
    ))
    conn.commit()
    trade_id = cur.lastrowid
    conn.close()
    return trade_id


# =========================
# PRICE HELPERS
# =========================
def fetch_coinbase_spot_price(product="BTC-USD"):
    url = f"https://api.coinbase.com/v2/prices/{product}/spot"
    response = requests.get(url, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    data = response.json()

    amount = safe_float(data.get("data", {}).get("amount"), None)
    if amount is None or amount <= 0:
        raise ValueError(f"Invalid price returned for {product}")

    return amount


def get_symbol_price(symbol):
    symbol = (symbol or DEFAULT_SYMBOL).upper().strip()

    if symbol in ["BTC/USD", "BTC-USD", "BTCUSD"]:
        return fetch_coinbase_spot_price("BTC-USD")
    if symbol in ["ETH/USD", "ETH-USD", "ETHUSD"]:
        return fetch_coinbase_spot_price("ETH-USD")

    raise ValueError(f"Unsupported symbol: {symbol}")


def get_live_prices():
    btc_price = fetch_coinbase_spot_price("BTC-USD")
    eth_price = fetch_coinbase_spot_price("ETH-USD")

    return {
        "data": [
            {"symbol": "BTC/USD", "price": btc_price},
            {"symbol": "ETH/USD", "price": eth_price}
        ],
        "updated_at": now_iso()
    }


# =========================
# STRATEGY STATE HELPERS
# =========================
def get_default_strategy_state():
    return {
        "signal": "HOLD",
        "reason": "Initializing",
        "momentum": 0,
        "ma5": 0,
        "ma15": 0,
        "suggested_size": 100,
        "risk_status": "NORMAL",
        "trading_locked": False,
        "stop_loss": 0,
        "take_profit": 0,
        "day_start_value": STARTING_CASH,
        "daily_pl": 0,
        "daily_pl_percent": 0,
        "updated_at": now_iso()
    }


def load_strategy_state():
    default_state = get_default_strategy_state()

    if not file_exists(STRATEGY_STATE_FILE):
        return default_state

    try:
        with open(STRATEGY_STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)

        if not isinstance(data, dict):
            return default_state

        merged = default_state.copy()
        merged.update(data)
        return merged
    except Exception:
        return default_state


def save_strategy_state(data):
    try:
        with open(STRATEGY_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        print(f"[WARN] Could not save strategy state: {e}")


def ensure_day_start_value(state, portfolio_total):
    today = datetime.now().date().isoformat()
    saved_day = state.get("day_date")

    if saved_day != today:
        state["day_date"] = today
        state["day_start_value"] = round(safe_float(portfolio_total, STARTING_CASH), 2)


def refresh_strategy_state_from_portfolio():
    state = load_strategy_state()
    portfolio = calculate_portfolio_stats()

    total_value = safe_float(portfolio.get("total_value"), STARTING_CASH)
    ensure_day_start_value(state, total_value)

    day_start_value = safe_float(state.get("day_start_value"), STARTING_CASH)
    if day_start_value <= 0:
        day_start_value = STARTING_CASH
        state["day_start_value"] = STARTING_CASH

    daily_pl = total_value - day_start_value
    daily_pl_percent = (daily_pl / day_start_value) * 100 if day_start_value > 0 else 0

    state["daily_pl"] = round(daily_pl, 2)
    state["daily_pl_percent"] = round(daily_pl_percent, 4)
    state["updated_at"] = now_iso()

    save_strategy_state(state)
    return state


# =========================
# PORTFOLIO CALCULATION
# =========================
def calculate_portfolio_stats():
    latest = get_latest_portfolio_row()

    if latest:
        cash_balance = safe_float(latest["cash_balance"], STARTING_CASH)
        btc_quantity = safe_float(latest["btc_quantity"], 0.0)
        fallback_btc_price = safe_float(latest["btc_price"], 0.0)
    else:
        cash_balance = STARTING_CASH
        btc_quantity = 0.0
        fallback_btc_price = 0.0

    try:
        btc_price = get_symbol_price("BTC/USD")
    except Exception:
        btc_price = fallback_btc_price

    btc_value = btc_quantity * btc_price
    total_value = cash_balance + btc_value

    return {
        "btc_price": round(btc_price, 2),
        "btc_quantity": round(btc_quantity, 8),
        "btc_value": round(btc_value, 2),
        "cash_balance": round(cash_balance, 2),
        "total_value": round(total_value, 2),
        "updated_at": now_iso()
    }


# =========================
# ROUTES
# =========================
@app.route("/api/health", methods=["GET"])
def health():
    return jsonify({
        "ok": True,
        "message": "Aurora backend is running",
        "timestamp": now_iso()
    })


@app.route("/api/prices", methods=["GET"])
def prices():
    try:
        return jsonify(get_live_prices())
    except Exception as e:
        return jsonify({
            "ok": False,
            "error": str(e)
        }), 500


@app.route("/api/portfolio-stats", methods=["GET"])
def portfolio_stats():
    try:
        stats = calculate_portfolio_stats()

        insert_portfolio_snapshot(
            btc_price=stats["btc_price"],
            btc_quantity=stats["btc_quantity"],
            btc_value=stats["btc_value"],
            cash_balance=stats["cash_balance"],
            total_value=stats["total_value"]
        )

        return jsonify(stats)
    except Exception as e:
        return jsonify({
            "ok": False,
            "error": str(e)
        }), 500


@app.route("/api/paper-history", methods=["GET"])
def paper_history():
    try:
        rows = get_trade_history(limit=200)

        history = []
        for row in rows:
            history.append({
                "id": row["id"],
                "side": row["side"],
                "symbol": row["symbol"],
                "price": round(safe_float(row["price"]), 2),
                "usd_amount": round(safe_float(row["usd_amount"]), 2),
                "asset_quantity": round(safe_float(row["asset_quantity"]), 8),
                "strategy_reason": row["strategy_reason"],
                "created_at": row["created_at"]
            })

        return jsonify({
            "ok": True,
            "history": history
        })
    except Exception as e:
        return jsonify({
            "ok": False,
            "error": str(e)
        }), 500


@app.route("/api/strategy-status", methods=["GET"])
def strategy_status():
    try:
        state = refresh_strategy_state_from_portfolio()
        return jsonify({
            "ok": True,
            "strategy": state
        })
    except Exception as e:
        return jsonify({
            "ok": False,
            "error": str(e)
        }), 500


@app.route("/api/strategy-status", methods=["POST"])
def update_strategy_status():
    try:
        data = request.get_json(silent=True)

        if not isinstance(data, dict):
            return jsonify({
                "ok": False,
                "error": "Invalid JSON payload"
            }), 400

        state = load_strategy_state()
        state.update(data)

        portfolio = calculate_portfolio_stats()
        total_value = safe_float(portfolio.get("total_value"), STARTING_CASH)
        ensure_day_start_value(state, total_value)

        day_start_value = safe_float(state.get("day_start_value"), STARTING_CASH)
        if day_start_value <= 0:
            day_start_value = STARTING_CASH
            state["day_start_value"] = STARTING_CASH

        daily_pl = total_value - day_start_value
        daily_pl_percent = (daily_pl / day_start_value) * 100 if day_start_value > 0 else 0

        state["daily_pl"] = round(daily_pl, 2)
        state["daily_pl_percent"] = round(daily_pl_percent, 4)
        state["updated_at"] = now_iso()

        save_strategy_state(state)

        return jsonify({
            "ok": True,
            "strategy": state
        })
    except Exception as e:
        return jsonify({
            "ok": False,
            "error": str(e)
        }), 500


@app.route("/api/paper-trade", methods=["POST"])
def paper_trade():
    try:
        data = request.get_json(silent=True)

        if not data:
            return jsonify({
                "ok": False,
                "error": "Expected JSON body"
            }), 400

        symbol = data.get("symbol", DEFAULT_SYMBOL)
        side = str(data.get("side", "")).lower().strip()
        usd_amount = safe_float(data.get("usd_amount"), None)
        strategy_reason = data.get("strategy_reason", "paper bot / api")

        if side not in ["buy", "sell"]:
            return jsonify({
                "ok": False,
                "error": "side must be 'buy' or 'sell'"
            }), 400

        if usd_amount is None or usd_amount <= 0:
            return jsonify({
                "ok": False,
                "error": "usd_amount must be greater than 0"
            }), 400

        current_stats = calculate_portfolio_stats()

        cash_balance = safe_float(current_stats["cash_balance"], 0.0)
        btc_quantity = safe_float(current_stats["btc_quantity"], 0.0)
        btc_price = safe_float(get_symbol_price(symbol), None)

        if btc_price is None or btc_price <= 0:
            return jsonify({
                "ok": False,
                "error": "Could not fetch live price"
            }), 500

        asset_quantity = usd_amount / btc_price

        if side == "buy":
            if cash_balance < usd_amount:
                return jsonify({
                    "ok": False,
                    "error": "Insufficient cash balance"
                }), 400

            new_cash_balance = cash_balance - usd_amount
            new_btc_quantity = btc_quantity + asset_quantity

        else:
            if btc_quantity <= 0:
                return jsonify({
                    "ok": False,
                    "error": "No BTC position to sell"
                }), 400

            if asset_quantity > btc_quantity:
                asset_quantity = btc_quantity
                usd_amount = asset_quantity * btc_price

            new_cash_balance = cash_balance + usd_amount
            new_btc_quantity = btc_quantity - asset_quantity

            if new_btc_quantity < 0:
                new_btc_quantity = 0.0

        new_btc_value = new_btc_quantity * btc_price
        new_total_value = new_cash_balance + new_btc_value

        trade_id = insert_trade(
            side=side,
            symbol=symbol,
            price=btc_price,
            usd_amount=usd_amount,
            asset_quantity=asset_quantity,
            strategy_reason=strategy_reason
        )

        insert_portfolio_snapshot(
            btc_price=btc_price,
            btc_quantity=new_btc_quantity,
            btc_value=new_btc_value,
            cash_balance=new_cash_balance,
            total_value=new_total_value
        )

        refresh_strategy_state_from_portfolio()

        return jsonify({
            "ok": True,
            "trade_id": trade_id,
            "side": side,
            "symbol": symbol,
            "price": round(btc_price, 2),
            "usd_amount": round(usd_amount, 2),
            "asset_quantity": round(asset_quantity, 8),
            "portfolio": {
                "btc_price": round(btc_price, 2),
                "btc_quantity": round(new_btc_quantity, 8),
                "btc_value": round(new_btc_value, 2),
                "cash_balance": round(new_cash_balance, 2),
                "total_value": round(new_total_value, 2),
                "updated_at": now_iso()
            }
        })
    except Exception as e:
        return jsonify({
            "ok": False,
            "error": str(e)
        }), 500


# =========================
# STARTUP
# =========================
init_db()

if __name__ == "__main__":
    print("Aurora backend starting on http://127.0.0.1:5000")
    app.run(host="127.0.0.1", port=5000, debug=True)