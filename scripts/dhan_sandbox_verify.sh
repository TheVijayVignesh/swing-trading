#!/usr/bin/env bash
# DhanHQ sandbox verification — Implementation Gate #1
# Uses ONLY https://sandbox.dhan.co/v2. No production endpoints. No real money.
set -u
source "$(dirname "$0")/../.env"
BASE="$DHAN_SANDBOX_BASE"
TOK="$DHAN_SANDBOX_TOKEN"
CID="$DHAN_CLIENT_ID"
EV=/tmp/dhan_evidence

req() { # req METHOD PATH [json_body] [extra_header]
  local m=$1 p=$2 b=${3:-} h=${4:-}
  local args=(-s -w '\n%{http_code}' -X "$m" "$BASE$p"
    -H 'Accept: application/json' -H 'Content-Type: application/json'
    -H "access-token: $TOK")
  [ -n "$h" ] && args+=(-H "$h")
  [ -n "$b" ] && args+=(-d "$b")
  curl "${args[@]}"
}

echo "== 1. AUTH/PROFILE: GET /profile =="
req GET /profile | tee $EV/01_profile.txt; echo

echo "== 1b. FUNDS: GET /fundlimit =="
req GET /fundlimit | tee $EV/02_fundlimit.txt; echo

echo "== 2. ORDERS BOOK: GET /orders =="
req GET /orders | tee $EV/03_orders.txt; echo

echo "== 3. MARKET DATA: POST /marketfeed/ltp (RELIANCE=2885, TCS=11536) =="
req POST /marketfeed/ltp '{"NSE_EQ":[2885,11536]}' "client-id: $CID" | tee $EV/04_ltp.txt; echo

echo "== 4. HISTORICAL DAILY (wide probe): RELIANCE 2000-01-01..2005-01-01 =="
req POST /charts/historical '{"securityId":"2885","exchangeSegment":"NSE_EQ","instrument":"EQUITY","expiryCode":0,"oi":false,"fromDate":"2000-01-01","toDate":"2005-01-01"}' | head -c 400 | tee $EV/05_hist_daily_probe.txt; echo

echo "== 4b. HISTORICAL DAILY (recent): RELIANCE 2026-08-01..2026-08-24 =="
req POST /charts/historical '{"securityId":"2885","exchangeSegment":"NSE_EQ","instrument":"EQUITY","expiryCode":0,"oi":false,"fromDate":"2026-08-01","toDate":"2026-08-24"}' | head -c 600 | tee $EV/06_hist_daily_recent.txt; echo

echo "== 4c. INTRADAY 5m: TCS last week =="
req POST /charts/intraday '{"securityId":"11536","exchangeSegment":"NSE_EQ","instrument":"EQUITY","interval":"5","oi":false,"fromDate":"2026-08-17 09:15:00","toDate":"2026-08-21 15:30:00"}' | head -c 500 | tee $EV/07_hist_intraday.txt; echo

echo "== 4d. INTRADAY 5y probe: TCS 2021-08..2021-09 =="
req POST /charts/intraday '{"securityId":"11536","exchangeSegment":"NSE_EQ","instrument":"EQUITY","interval":"5","oi":false,"fromDate":"2021-08-02 09:15:00","toDate":"2021-08-31 15:30:00"}' | head -c 300 | tee $EV/08_intraday_5y_probe.txt; echo

echo "== 5. PLACE ORDER: LIMIT BUY TCS deep below market =="
CORR="gate1-$(date +%s)"
BODY=$(cat <<EOF
{"dhanClientId":"$CID","correlationId":"$CORR","transactionType":"BUY","exchangeSegment":"NSE_EQ","productType":"CNC","orderType":"LIMIT","validity":"DAY","securityId":"11536","quantity":1,"disclosedQuantity":0,"price":10.05,"triggerPrice":0,"afterMarketOrder":false,"amoTime":"","boProfitValue":0,"boStopLossValue":0}
EOF
)
echo "$BODY" > $EV/09_place_req.json
req POST /orders "$BODY" | tee $EV/09_place_resp.txt; echo
