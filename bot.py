import os
import re
import json
import requests
import logging

# ============ CẤU HÌNH ============
# Đọc từ biến môi trường (GitHub Secrets) hoặc dùng giá trị mặc định
USERNAME = os.environ.get("BDS_USERNAME", "22124072")
PASSWORD = os.environ.get("BDS_PASSWORD", "12345")

BUY_DIP_PERCENT  = 5.0   # Mua khi giá giảm X% so với đỉnh gần đây
SELL_MIN_PROFIT  = 1.0   # Bán khi có lời ít nhất X%

# ============ SETUP ============
BASE_URL   = "https://bdsnl.com/bdstechv2/game/api/market_api.jsp"
PAGE_URL   = "https://bdsnl.com/bdstechv2/game/market.jsp"
LOGIN_URL  = "https://bdsnl.com/bdstechv2/api/login.jsp"
STATE_FILE = "state.json"  # Lưu peak_prices giữa các lần chạy

session = requests.Session()
session.headers.update({
    "Referer": PAGE_URL,
    "Accept": "application/json, text/plain, */*",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
})

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)


def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                return json.load(f)
        except Exception:
            pass
    return {"peak_prices": {}}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def login():
    try:
        r = session.post(LOGIN_URL,
            data={"username": USERNAME, "password": PASSWORD},
            timeout=15, allow_redirects=True)
        if "login" in r.url.lower():
            log.error("❌ Đăng nhập thất bại! Sai username/password?")
            return False
        log.info(f"✅ Đăng nhập thành công: {USERNAME}")
        return True
    except Exception as e:
        log.error(f"Lỗi đăng nhập: {e}")
        return False


def get_balance():
    try:
        r = session.get(PAGE_URL, timeout=10)
        m = re.search(r'var\s+myXu\s*=\s*(\d+)', r.text)
        if m:
            return int(m.group(1))
    except Exception as e:
        log.error(f"Lỗi lấy balance: {e}")
    return 0


def get_market_list():
    try:
        r = session.get(BASE_URL, params={"action": "list"}, timeout=10)
        data = r.json()
        return data if isinstance(data, list) else []
    except Exception as e:
        log.error(f"Lỗi lấy danh sách: {e}")
        return []


def get_events():
    try:
        r = session.get(BASE_URL, params={"action": "market_status"}, timeout=10)
        d = r.json()
        return d.get("recent_events") or []
    except Exception:
        return []


def find_boom_target(events, properties):
    boom_names = []
    for ev in events:
        if ev.get("type") == "boom" and ev.get("secs_ago", 9999) < 60:
            title = ev.get("title", "")
            name = title.replace("🔥", "").replace("sốt giá!", "").strip()
            if name:
                boom_names.append(name.lower())
    if not boom_names:
        return None
    for prop in properties:
        for boom in boom_names:
            if boom in prop["name"].lower() and prop.get("available", 0) > 0:
                log.info(f"🔥 Phát hiện sự kiện SOT GIA: {prop['name']}")
                return prop
    return None


def buy(pid, qty):
    try:
        r = session.get(BASE_URL, params={"action": "buy", "property_id": pid, "qty": qty}, timeout=10)
        return r.json()
    except Exception as e:
        log.error(f"Lỗi mua {pid}: {e}")
        return None


def sell_all(pid, qty):
    try:
        r = session.get(BASE_URL, params={"action": "sell", "property_id": pid, "qty": qty}, timeout=10)
        return r.json()
    except Exception as e:
        log.error(f"Lỗi bán {pid}: {e}")
        return None


def run_once():
    log.info("=== BOT BĐS - 1 TICK ===")

    if not login():
        return

    properties = get_market_list()
    if not properties:
        log.error("Không lấy được danh sách BĐS")
        return

    state = load_state()
    peak_prices = state.get("peak_prices", {})

    # Cập nhật peak prices
    for prop in properties:
        pid = str(prop["id"])
        price = float(prop["price"])
        if price > 0 and (pid not in peak_prices or price > peak_prices[pid]):
            peak_prices[pid] = price

    # === LOGIC BÁN ===
    portfolio = [prop for prop in properties if prop.get("my_qty", 0) > 0]
    for prop in portfolio:
        pid = str(prop["id"])
        name = prop["name"]
        current = float(prop["price"])
        bought_at = float(prop["my_buy"])
        qty = int(prop["my_qty"])
        if bought_at > 0:
            profit_pct = (current - bought_at) / bought_at * 100
            if profit_pct >= SELL_MIN_PROFIT:
                log.info(f"BÁN {name} x{qty}: giá {current:,.0f} (+{profit_pct:.1f}% | lãi {prop['my_profit']:+,} XU)")
                result = sell_all(pid, qty)
                log.info(f"Kết quả: {result}")

    # === LOGIC MUA ===
    properties = get_market_list()
    portfolio_after = [p for p in properties if p.get("my_qty", 0) > 0]
    if not portfolio_after:
        balance = get_balance()
        log.info(f"💰 Số dư: {balance:,} XU")

        bought = False
        # ƯU TIÊN 1: Mua sốt giá
        events = get_events()
        boom_prop = find_boom_target(events, properties)
        if boom_prop:
            price = float(boom_prop["price"])
            available = int(boom_prop.get("available", 0))
            max_qty = min(int(balance // price), available)
            if max_qty > 0:
                log.info(f"🔥 MUA SOT GIA {boom_prop['name']} x{max_qty}: tổng {max_qty*price:,.0f} XU")
                log.info(f"Kết quả: {buy(str(boom_prop['id']), max_qty)}")
                bought = True

        # ƯU TIÊN 2: Mua BĐS đang giảm giá
        if not bought:
            candidates = []
            for prop in properties:
                pid = str(prop["id"])
                price = float(prop["price"])
                available = int(prop.get("available", 0))
                if price <= 0 or available <= 0:
                    continue
                peak = peak_prices.get(pid, price)
                dip_pct = (peak - price) / peak * 100
                if dip_pct >= BUY_DIP_PERCENT:
                    max_qty = min(int(balance // price), available)
                    if max_qty > 0:
                        candidates.append({
                            "prop": prop, "pid": pid, "price": price,
                            "dip_pct": dip_pct, "max_qty": max_qty,
                            "total_cost": max_qty * price,
                        })
            if candidates:
                best = max(candidates, key=lambda x: x["total_cost"])
                log.info(f"MUA {best['prop']['name']} x{best['max_qty']}: giá {best['price']:,.0f} | giảm {best['dip_pct']:.1f}% | tổng {best['total_cost']:,.0f} XU")
                log.info(f"Kết quả: {buy(best['pid'], best['max_qty'])}")
            else:
                log.info("Không có BĐS nào đủ điều kiện mua.")

    # Lưu state
    state["peak_prices"] = peak_prices
    save_state(state)

    # Báo cáo cuối
    properties = get_market_list()
    holding = [f"{p['name']}x{p['my_qty']}" for p in properties if p.get("my_qty", 0) > 0]
    balance = get_balance()
    log.info(f"📊 Số dư cuối: {balance:,} XU | Đang giữ: {', '.join(holding) or 'trống'}")


if __name__ == "__main__":
    run_once()
