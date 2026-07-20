#!/usr/bin/env bash

set -eo pipefail

MCP_URL="${MCP_URL:-http://127.0.0.1:8000/mcp}"
ACCEPT_HEADER='Accept: application/json, text/event-stream'
CONTENT_TYPE_HEADER='Content-Type: application/json'
SESSION_ID=""
REQ_ID=1

print_divider() {
  echo "========================================"
}

extract_jsonrpc_data() {
  sed -n 's/^data: //p' /tmp/mcp_body | tail -n 1
}

initialize_session() {
  local payload
  payload='{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"tmp-seed-script","version":"1.0"}}}'

  curl -sS -D /tmp/mcp_hdr -o /tmp/mcp_body -X POST "$MCP_URL" \
    -H "$ACCEPT_HEADER" \
    -H "$CONTENT_TYPE_HEADER" \
    --data "$payload"

  SESSION_ID="$(awk 'BEGIN{IGNORECASE=1} /^mcp-session-id:/ {gsub("\r", "", $2); print $2}' /tmp/mcp_hdr | tail -n 1)"
  if [[ -z "$SESSION_ID" ]]; then
    echo "Failed to obtain mcp-session-id from initialize response"
    cat /tmp/mcp_body
    exit 1
  fi

  echo "Initialized MCP session: $SESSION_ID"
}

call_tool() {
  local tool_name="$1"
  local args_json="$2"

  local payload
  payload="{\"jsonrpc\":\"2.0\",\"id\":$REQ_ID,\"method\":\"tools/call\",\"params\":{\"name\":\"$tool_name\",\"arguments\":$args_json}}"

  curl -sS -D /tmp/mcp_hdr -o /tmp/mcp_body -X POST "$MCP_URL" \
    -H "$ACCEPT_HEADER" \
    -H "$CONTENT_TYPE_HEADER" \
    -H "Mcp-Session-Id: $SESSION_ID" \
    --data "$payload"

  local data_json
  data_json="$(extract_jsonrpc_data)"

  if [[ -z "$data_json" ]]; then
    echo "Empty MCP response body for tool call: $tool_name"
    cat /tmp/mcp_body
    exit 1
  fi

  if echo "$data_json" | grep -q '"error"'; then
    echo "Tool call returned error: $tool_name"
    echo "$data_json"
    return 1
  fi

  if echo "$data_json" | grep -q '"isError":true'; then
    echo "Tool call returned isError=true: $tool_name"
    echo "$data_json"
    return 1
  fi

  echo "$data_json"
  REQ_ID=$((REQ_ID + 1))
  return 0
}

call_sql_execute_allow_error() {
  local statement="$1"
  local args_json
  args_json="{\"statement\":\"$statement\",\"confirm_write\":true}"

  if ! call_tool "sql_execute" "$args_json" >/dev/null; then
    echo "Continuing after non-fatal sql_execute error: $statement"
  fi
}

call_sql_execute_required() {
  local statement="$1"
  local args_json
  args_json="{\"statement\":\"$statement\",\"confirm_write\":true}"

  call_tool "sql_execute" "$args_json" >/dev/null
  echo "OK execute: $statement"
}

call_sql_query_required() {
  local statement="$1"
  local args_json
  args_json="{\"statement\":\"$statement\"}"

  local result
  result="$(call_tool "sql_query" "$args_json")"
  echo "$result"
}

print_divider
echo "Step 1: Initialize MCP session"
initialize_session

print_divider
echo "Step 2: Drop prior demo tables if they exist"
call_sql_execute_allow_error "DROP TABLE erp_order_lines_demo"
call_sql_execute_allow_error "DROP TABLE erp_orders_demo"
call_sql_execute_allow_error "DROP TABLE erp_customers_demo"

print_divider
echo "Step 3: Create demo ERP tables"
call_sql_execute_required "CREATE TABLE erp_customers_demo (customer_id INTEGER PRIMARY KEY, customer_name VARCHAR(100), tier VARCHAR(20))"
call_sql_execute_required "CREATE TABLE erp_orders_demo (order_id INTEGER PRIMARY KEY, customer_id INTEGER, order_total DECIMAL(12,2), order_status VARCHAR(20), order_date DATE)"
call_sql_execute_required "CREATE TABLE erp_order_lines_demo (line_id INTEGER PRIMARY KEY, order_id INTEGER, sku VARCHAR(40), qty INTEGER, unit_price DECIMAL(12,2))"

print_divider
echo "Step 4: Insert sample ERP rows"
call_sql_execute_required "INSERT INTO erp_customers_demo (customer_id, customer_name, tier) VALUES (1, 'Acme Health', 'GOLD')"
call_sql_execute_required "INSERT INTO erp_customers_demo (customer_id, customer_name, tier) VALUES (2, 'Northwind Retail', 'SILVER')"
call_sql_execute_required "INSERT INTO erp_customers_demo (customer_id, customer_name, tier) VALUES (3, 'Blue Sky Foods', 'BRONZE')"

call_sql_execute_required "INSERT INTO erp_orders_demo (order_id, customer_id, order_total, order_status, order_date) VALUES (1001, 1, 1200.00, 'OPEN', '2026-07-01')"
call_sql_execute_required "INSERT INTO erp_orders_demo (order_id, customer_id, order_total, order_status, order_date) VALUES (1002, 1, 450.00, 'SHIPPED', '2026-07-05')"
call_sql_execute_required "INSERT INTO erp_orders_demo (order_id, customer_id, order_total, order_status, order_date) VALUES (1003, 2, 780.00, 'OPEN', '2026-07-10')"

call_sql_execute_required "INSERT INTO erp_order_lines_demo (line_id, order_id, sku, qty, unit_price) VALUES (1, 1001, 'SKU-AX1', 10, 60.00)"
call_sql_execute_required "INSERT INTO erp_order_lines_demo (line_id, order_id, sku, qty, unit_price) VALUES (2, 1001, 'SKU-BB2', 12, 50.00)"
call_sql_execute_required "INSERT INTO erp_order_lines_demo (line_id, order_id, sku, qty, unit_price) VALUES (3, 1003, 'SKU-AX1', 6, 65.00)"

print_divider
echo "Step 5: Validate via MCP sql_query"
echo "Query A: customer count"
call_sql_query_required "SELECT COUNT(*) AS customer_count FROM erp_customers_demo"

echo "Query B: open order totals by customer"
call_sql_query_required "SELECT c.customer_name, SUM(o.order_total) AS open_total FROM erp_orders_demo o JOIN erp_customers_demo c ON c.customer_id = o.customer_id WHERE o.order_status = 'OPEN' GROUP BY c.customer_name ORDER BY open_total DESC"

echo "Query C: SKU demand"
call_sql_query_required "SELECT sku, SUM(qty) AS total_qty FROM erp_order_lines_demo GROUP BY sku ORDER BY total_qty DESC"

print_divider
echo "Seed and MCP verification complete."
echo "Demo tables: erp_customers_demo, erp_orders_demo, erp_order_lines_demo"
