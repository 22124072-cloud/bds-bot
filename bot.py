import requests
import re
import time
import logging

# ============ CẤU HÌNH ============
USERNAME = "22124072"   # Thay bằng tên đăng nhập của bạn
PASSWORD = "12345"      # Thay bằng mật khẩu của bạn

BUY_DIP_PERCENT  = 5.0   # Mua khi giá giảm X% so với đỉnh gần đây
SELL_MIN_PROFIT  = 1.0   # Bán khi có lời ít nhất X%
TICK_INTERVAL    = 305

# ============ SETUP ============
BASE_URL   = "https://bdsnl.com/bdstechv2/game/api/market_api.jsp"
PAGE_URL   = "https://bdsnl.com/bdstechv2/game/market.jsp"
LOGIN_URL  = "https://bdsnl.com/bdstechv2/api/login.jsp"

# Dùng session để tự lưu cookie
session = requests.Session()
session.headers.update({
    "Referer": PAGE_URL,
    "Accept": "application/json, text/plain, */*",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
})

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("bot.log", encoding="utf-8"),
        logging.StreamHandler()
    ]
)
log = logging.getLogger(__name__)

peak_prices = {}  # pid -> giá cao nhất đã thấy


def get_balance():
    """Lấy số dư XU từ trang HTML."""
    try:
        r = session.get(PAGE_URL, timeout=10)
        m = re.search(r'var\s+myXu\s*=\s*(\d+)', r.text)
        if m:
            return int(m.group(1))
    except Exception as e:
        log.error(f"Lỗi lấy balance: {e}")
    return 0


def login():
    """Đăng nhập bằng username/password, session sẽ tự lưu cookie."""
    try:
        r = session.post(LOGIN_URL,
            data={"username": USERNAME, "password": PASSWORD},
            timeout=15, allow_redirects=True)
        # Sau khi login thành công sẽ được redirect về dashboard
        if "login" in r.url.lower():
            log.error("❌ Đăng nhập thất bại! Sai username/password?")
            return False
        log.info(f"✅ Đăng nhập thành công! User: {USERNAME}")
        return True
    except Exception as e:
        log.error(f"Lỗi đăng nhập: {e}")
        return False


def is_logged_in():
    """Kiểm tra session còn hợp lệ không."""
    try:
        r = session.get(PAGE_URL, timeout=10, allow_redirects=False)
        if r.status_code in (301, 302):
            return False
        if "login" in r.url.lower():
            return False
        return True
    except Exception:
        return False


def ensure_logged_in():
    """Đảm bảo đã đăng nhập, tự login lại nếu cần."""
    if not is_logged_in():
        log.warning("⚠️ Session hết hạn, đang đăng nhập lại...")
        return login()
    return True


def get_market_list():
    try:
        r = session.get(BASE_URL, params={"action": "list"}, timeout=10)
        data = r.json()
        return data if isinstance(data, list) else []
    except Exception as e:
        log.error(f"Lỗi lấy danh sách: {e}")
        return []


def get_next_tick_secs():
    try:
        r = session.get(BASE_URL, params={"action": "tick_prices"}, timeout=10)
        d = r.json()
        secs = d.get("next_tick_secs")
        if secs and 5 < secs < 400:
            return int(secs)
    except Exception as e:
        log.error(f"Lỗi lấy tick: {e}")
    return TICK_INTERVAL


def get_events():
    """Lấy sự kiện thị trường, trả về list events mới nhất."""
    try:
        r = session.get(BASE_URL, params={"action": "market_status"}, timeout=10)
        d = r.json()
        return d.get("recent_events") or []
    except Exception as e:
        log.error(f"Lỗi lấy events: {e}")
    return []


def find_boom_target(events, properties):
    """Tìm BĐS được dự đoán sắp sốt giá từ sự kiện mới (< 60 giây trước tick)."""
    boom_names = []
    for ev in events:
        if ev.get("type") == "boom" and ev.get("secs_ago", 9999) < 60:
            title = ev.get("title", "")
            # Trích tên BĐS từ tiêu đề, vd: "🔥 Căn hộ Sunshine City sốt giá!"
            name = title.replace("🔥", "").replace("sốt giá!", "").strip()
            if name:
                boom_names.append(name.lower())

    if not boom_names:
        return None

    # Tìm BĐS khớp tên trong danh sách
    for prop in properties:
        for boom in boom_names:
            if boom in prop["name"].lower():
                if prop.get("available", 0) > 0:
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


def run_bot():
    log.info("=== BOT BĐS ẢO BẮT ĐẦU CHẠY ===")
    log.info(f"Mua khi giảm: {BUY_DIP_PERCENT}% | Bán khi lãi: {SELL_MIN_PROFIT}%")

    # Đăng nhập lần đầu
    if not login():
        log.error("❌ Không đăng nhập được! Kiểm tra USERNAME/PASSWORD.")
        return

    while True:
        try:
            log.info("--- Tick mới ---")

            # Tự động đăng nhập lại nếu session hết hạn
            if not ensure_logged_in():
                log.error("Không đăng nhập được, chờ 60s thử lại...")
                time.sleep(60)
                continue

            properties = get_market_list()
            if not properties:
                log.warning("Không lấy được danh sách, thử lại sau 60s...")
                time.sleep(60)
                continue

            # Cập nhật peak prices
            for prop in properties:
                pid = str(prop["id"])
                price = float(prop["price"])
                if price > 0 and (pid not in peak_prices or price > peak_prices[pid]):
                    peak_prices[pid] = price

            # Danh sách đang giữ (từ my_qty trong list)
            portfolio = [prop for prop in properties if prop.get("my_qty", 0) > 0]

            # === LOGIC BÁN ===
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
                        log.info(f"Kết quả bán: {result}")

            # === LOGIC MUA ===
            # Chỉ mua khi không giữ gì
            portfolio_after_sell = [p for p in properties if p.get("my_qty", 0) > 0]
            if not portfolio_after_sell:
                balance = get_balance()
                log.info(f"Số dư: {balance:,} XU")

                bought = False

                # ƯU TIÊN 1: Mua BĐS sắp sốt giá (event boom mới < 60s)
                events = get_events()
                boom_prop = find_boom_target(events, properties)
                if boom_prop:
                    price = float(boom_prop["price"])
                    available = int(boom_prop.get("available", 0))
                    max_qty = min(int(balance // price), available)
                    if max_qty > 0:
                        log.info(f"🔥 MUA SOT GIA {boom_prop['name']} x{max_qty}: giá {price:,.0f} | tổng {max_qty*price:,.0f} XU")
                        result = buy(str(boom_prop["id"]), max_qty)
                        log.info(f"Kết quả: {result}")
                        bought = True

                # ƯU TIÊN 2: Mua BĐS đang giảm giá (chiến lược thông thường)
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
                                    "prop": prop,
                                    "pid": pid,
                                    "price": price,
                                    "dip_pct": dip_pct,
                                    "max_qty": max_qty,
                                    "total_cost": max_qty * price,
                                })

                    if candidates:
                        best = max(candidates, key=lambda x: x["total_cost"])
                        name = best["prop"]["name"]
                        log.info(f"MUA {name} x{best['max_qty']}: giá {best['price']:,.0f} | giảm {best['dip_pct']:.1f}% | tổng {best['total_cost']:,.0f} XU")
                        result = buy(best["pid"], best["max_qty"])
                        log.info(f"Kết quả mua: {result}")
                    else:
                        log.info("Không có BĐS nào đủ điều kiện mua.")

            # Lấy lại portfolio sau khi mua/bán
            properties2 = get_market_list()
            portfolio_final = [p for p in properties2 if p.get("my_qty", 0) > 0]
            balance_final = get_balance()

            holding_str = ", ".join([f"{p['name']}x{p['my_qty']}" for p in portfolio_final]) or "trống"
            log.info(f"Số dư: {balance_final:,} XU | Đang giữ: {holding_str}")


            # Chờ đến tick tiếp theo nếu không giữ gì, ngược lại check nhanh
            if portfolio_final:
                log.info("Đang giữ BĐS, check lại sau 2s...")
                time.sleep(2)
            else:
                wait = get_next_tick_secs()
                log.info(f"Chờ {wait}s đến tick tiếp theo...")
                time.sleep(wait)

        except KeyboardInterrupt:
            log.info("Bot dừng.")
            break
        except Exception as e:
            log.error(f"Lỗi: {e}")
            time.sleep(30)


if __name__ == "__main__":
    run_bot()
