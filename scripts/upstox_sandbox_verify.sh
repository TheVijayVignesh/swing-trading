#!/usr/bin/env bash
# Upstox sandbox lifecycle verification — SANDBOX GATE
# Uses ONLY https://api-sandbox.upstox.com. No production endpoints. No real money.
set -u
: "${UPSTOX_SANDBOX_TOKEN:?export UPSTOX_SANDBOX_TOKEN=<token> first}"
BASE="https://api-sandbox.upstox.com"
TOK="$UPSTOX_SANDBOX_TOKEN"
EV=/tmp/upstox_evidence
mkdir -p "$EV"

req() { # req METHOD PATH [body]
  local m=$1 p=$2 b=${3:-}
  local args=(-s -w '\nHTTP:%{http_code}' -X "$m" "$BASE$p"
    -H 'Accept: application/json' -H "Authorization: Bearer $TOK")
  [ -n "$b" ] && args+=(-H 'Content-Type: application/json' -d "$b")
  curl "${args[@]}"
}

echo "== A. READS (the decisive unknown) =="
echo "-- profile:";        req GET /v2/profile | tee $EV/a_profile.txt;   echo
echo "-- user funds:";    req GET /v2/user/get-funds-and-margin | tee $EV/a_funds.txt; echo
echo "-- order book:";    req GET /v2/order/book | tee $EV/a_orderbook_before.txt; echo
echo "-- trades:";        req GET /v2/order/trades | tee $EV/a_trades.txt; echo
echo "-- positions:";     req GET /v2/portfolio/short-term-positions | tee $EV/a_positions.txt; echo

echo "== B. PLACE (deep off-market LIMIT BUY, NSE_EQ RELIANCE ISIN) =="
CORR="gate1-$(date +%s)"
BODY='{"quantity":1,"product":"I","validity":"DAY","price":10.05,"tag":"'"$CORR"'",
       "instrument_token":"NSE_EQ|INE002A01018","order_type":"LIMIT",
       "transaction_type":"BUY","disclosed_quantity":0,"trigger_price":0,"is_amo":false}'
PLACE=$(req POST /v2/order/place "$BODY"); echo "$PLACE" | tee $EV/b_place.txt; echo
OID=$(echo "$PLACE" | sed -n 's/.*"order_id":"\([0-9a-zA-Z]*\)".*/\1/p')
echo "extracted orderId=$OID"

echo "== C. QUERY after place =="
sleep 3
echo "-- order book:";    req GET /v2/order/book | tee $EV/c_book_after_place.txt; echo
if [ -n "$OID" ]; then
  echo "-- order details:"; req GET "/v2/order/details?order_id=$OID" | tee $EV/c_details.txt; echo
fi

echo "== D. MODIFY =="
if [ -n "$OID" ]; then
  MBODY='{"quantity":1,"validity":"DAY","price":10.10,"order_id":"'"$OID"'",
          "order_type":"LIMIT","transaction_type":"BUY","disclosed_quantity":0,
          "trigger_price":0,"instrument_token":"NSE_EQ|INE002A01018"}'
  req PUT /v2/order/modify "$MBODY" | tee $EV/d_modify.txt; echo
fi

echo "== E. CANCEL + verify final state =="
if [ -n "$OID" ]; then
  req DELETE "/v2/order/cancel?order_id=$OID" | tee $EV/e_cancel.txt; echo
  sleep 3
  req GET "/v2/order/details?order_id=$OID" | tee $EV/e_verify_final.txt; echo
fi

echo "== F. MARKET-order policy probe (accepted vs rejected?) =="
MB='{"quantity":1,"product":"I","validity":"DAY","price":0,"tag":"mkt-probe",
     "instrument_token":"NSE_EQ|INE002A01018","order_type":"MARKET",
     "transaction_type":"SELL","disclosed_quantity":0,"trigger_price":0,"is_amo":false}'
req POST /v2/order/place "$MB" | tee $EV/f_market_probe.txt; echo
echo "== done — evidence in $EV =="
