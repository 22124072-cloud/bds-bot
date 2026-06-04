import os
import re
import json
import time
import requests
import logging

# ============ CẤU HÌNH ============
# Đọc từ biến môi trường (GitHub Secrets) hoặc dùng giá trị mặc định
USERNAME = os.environ.get("BDS_USERNAME", "22124072")
PASSWORD = os.environ.get("BDS_PASSWORD", "12345")

BUY_DIP_PERCENT      = 3.0   # Mua khi giá giảm X% (giảm xuống để mua nhiều cơ hội hơn)
SELL_MIN_PROFIT      = 1.5   # Bán khi có lời X% (BULL/NEUTRAL)
SELL_MIN_PROFIT_BEAR = 0.3   # Bán nhanh khi BEAR (bảo toàn vốn)
SELL_CUT_LOSS_BEAR   = -3.0  # Cắt lỗ khi BEAR và lỗ X%

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


def get_market_status():
    try:
        r = session.get(BASE_URL, params={"action": "market_status"}, timeout=10)
        return r.json()
    except Exception:
        return {}


def find_event_target(events, properties):
    """Tìm BĐS có sự kiện tích cực (boom/policy/infra với delta > 5%) mới < 60s."""
    target_names = []
    for ev in events:
        if ev.get("secs_ago", 9999) < 60 and ev.get("delta", 0) >= 5.0:
            ev_type = ev.get("type", "")
            if ev_type in ("boom", "policy", "infra"):
                title = ev.get("title", "")
                # Loại bỏ emoji + từ khóa
                name = re.sub(r'[^\w\sÀ-ỹ]', '', title).strip()
                for kw in ["sốt giá", "Chính sách hỗ trợ", "Hạ tầng mới gần", "BĐS mở bán đợt mới"]:
                    name = name.replace(kw, "").strip()
                if name and len(name) > 3:
                    target_names.append((name.lower(), ev.get("delta", 0), ev_type))

    if not target_names:
        return None

    # Tìm BĐS khớp
    for prop in properties:
        for name, delta, ev_type in target_names:
            if name in prop["name"].lower() and prop.get("available", 0) > 0:
                log.info(f"🔥 Sự kiện {ev_type} (+{delta}%): {prop['name']}")
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


def get_next_tick_secs():
    try:
        r = session.get(BASE_URL, params={"action": "tick_prices"}, timeout=10)
        d = r.json()
        secs = d.get("next_tick_secs")
        if secs and 0 < secs < 400:
            return int(secs)
    except Exception:
        pass
    return 0


def run_once():
    log.info("=== BOT BĐS - 1 TICK ===")

    if not login():
        return

    # Chạy nhanh, không chờ tick (tiết kiệm quota)
    # Cron 5 phút đã đủ đồng bộ với tick game 5 phút

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

    # Lấy tâm lý thị trường
    market = get_market_status()
    sentiment = market.get("sentiment", "neutral")
    log.info(f"📊 Thị trường: {sentiment.upper()}")

    # Điều chỉnh ngưỡng theo sentiment
    if sentiment == "bear":
        sell_threshold = SELL_MIN_PROFIT_BEAR
        cut_loss = SELL_CUT_LOSS_BEAR
    else:
        sell_threshold = SELL_MIN_PROFIT
        cut_loss = -999  # Không cắt lỗ khi BULL/NEUTRAL

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
            # Bán khi lãi đủ ngưỡng HOẶC cắt lỗ khi BEAR
            if profit_pct >= sell_threshold:
                log.info(f"BÁN LÃI {name} x{qty}: giá {current:,.0f} (+{profit_pct:.1f}% | lãi {prop['my_profit']:+,} XU)")
                log.info(f"Kết quả: {sell_all(pid, qty)}")
            elif profit_pct <= cut_loss:
                log.info(f"⚠️ CẮT LỖ {name} x{qty}: giá {current:,.0f} ({profit_pct:.1f}% | lỗ {prop['my_profit']:+,} XU)")
                log.info(f"Kết quả: {sell_all(pid, qty)}")

    # === LOGIC MUA ===
    properties = get_market_list()
    portfolio_after = [p for p in properties if p.get("my_qty", 0) > 0]
    if not portfolio_after:
        balance = get_balance()
        log.info(f"💰 Số dư: {balance:,} XU")

        bought = False
        # Nếu BEAR market → không mua, chờ đáy
        if sentiment == "bear":
            log.info("🐻 BEAR market - tạm không mua, chờ thị trường ổn định")
        else:
            # ƯU TIÊN 1: Mua theo sự kiện tốt (boom/policy/infra)
            events = market.get("recent_events", [])
            event_prop = find_event_target(events, properties)
            if event_prop:
                price = float(event_prop["price"])
                available = int(event_prop.get("available", 0))
                max_qty = min(int(balance // price), available)
                if max_qty > 0:
                    log.info(f"🔥 MUA SỰ KIỆN {event_prop['name']} x{max_qty}: tổng {max_qty*price:,.0f} XU")
                    log.info(f"Kết quả: {buy(str(event_prop['id']), max_qty)}")
                    bought = True

        # ƯU TIÊN 2: Mua BĐS đang giảm giá (chỉ khi không BEAR)
        if not bought and sentiment != "bear":
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
