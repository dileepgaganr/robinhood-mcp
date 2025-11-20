import robin_stocks.robinhood as r
import os
import sys
import json
import pyotp
from dotenv import load_dotenv

load_dotenv()

def login():
    access_token = os.getenv("ROBINHOOD_ACCESS_TOKEN")
    if access_token:
        r.helper.SESSION.headers["Authorization"] = access_token 
        if not access_token.startswith("Bearer "):
            access_token = "Bearer " + access_token
        r.helper.SESSION.headers["Authorization"] = access_token
        r.helper.set_login_state(True)
        return True, "Logged in with Access Token"

    user = os.getenv("ROBINHOOD_USER")
    password = os.getenv("ROBINHOOD_PASSWORD")
    mfa_secret = os.getenv("ROBINHOOD_MFA_SECRET")
    
    if not user or not password:
        return False, "Missing credentials"

    try:
        if mfa_secret:
            totp = pyotp.TOTP(mfa_secret).now()
            r.login(user, password, mfa_code=totp)
        else:
            r.login(user, password)
        return True, "Logged in"
    except Exception as e:
        return False, str(e)

def handle_command(data):
    command = data.get("command")
    args = data.get("args", [])
    
    if command == "quote":
        return r.stocks.get_stock_quote_by_symbol(args[0])
    elif command == "portfolio":
        profile = r.profiles.load_portfolio_profile()
        account = r.profiles.load_account_profile()
        return {"portfolio": profile, "account": account}
    elif command == "positions":
        return r.account.build_holdings()
    elif command == "buy":
        return r.orders.order_buy_market(args[0], float(args[1]))
    elif command == "sell":
        return r.orders.order_sell_market(args[0], float(args[1]))
    elif command == "orders":
        return r.orders.get_all_stock_orders()[:10]
    elif command == "instrument":
        return r.stocks.get_instruments_by_symbols(args[0])
    elif command == "watchlists":
        return r.account.get_all_watchlists()
    else:
        raise ValueError(f"Unknown command: {command}")

def main():
    success, message = login()
    if not success:
        sys.stderr.write(f"Login failed: {message}\n")
        sys.stderr.flush()

    for line in sys.stdin:
        if not line.strip():
            continue
        
        try:
            request = json.loads(line)
            response_data = handle_command(request)
            print(json.dumps({"status": "success", "data": response_data}))
        except Exception as e:
            print(json.dumps({"status": "error", "error": str(e)}))
        
        sys.stdout.flush()

if __name__ == "__main__":
    main()
