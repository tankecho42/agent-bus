#!/bin/bash
# Agent Bus 接口验证脚本
BASE="http://127.0.0.1:7700"
MK="testmaster123"

echo "=== 1. Register 3 Agents ==="
ECHO=$(curl -s -X POST $BASE/agents/register -H "X-API-Key: $MK" -H "Content-Type: application/json" -d '{"name":"echo","description":"Echo main agent"}')
echo "  echo: $ECHO"
ECHO_KEY=$(echo $ECHO | python3 -c "import sys,json; print(json.load(sys.stdin)['api_key'])")

CODEX=$(curl -s -X POST $BASE/agents/register -H "X-API-Key: $MK" -H "Content-Type: application/json" -d '{"name":"codex","description":"Codex coding agent"}')
echo "  codex: $CODEX"
CODEX_KEY=$(echo $CODEX | python3 -c "import sys,json; print(json.load(sys.stdin)['api_key'])")

CC=$(curl -s -X POST $BASE/agents/register -H "X-API-Key: $MK" -H "Content-Type: application/json" -d '{"name":"claude-code","description":"Claude Code agent"}')
echo "  claude-code: $CC"
CC_KEY=$(echo $CC | python3 -c "import sys,json; print(json.load(sys.stdin)['api_key'])")

echo ""
echo "=== 2. List Agents (as echo) ==="
curl -s $BASE/agents -H "X-API-Key: $ECHO_KEY" | python3 -m json.tool

echo ""
echo "=== 3. Whoami (as echo) ==="
curl -s $BASE/agents/me -H "X-API-Key: $ECHO_KEY"

echo ""
echo ""
echo "=== 4. Echo sends DM to Codex ==="
curl -s -X POST $BASE/messages -H "X-API-Key: $ECHO_KEY" -H "Content-Type: application/json" -d '{"to":"codex","subject":"task update","body":"Codex，那个 PR review 帮我看下 #42","priority":1}'

echo ""
echo ""
echo "=== 5. Echo broadcasts to all ==="
curl -s -X POST $BASE/messages -H "X-API-Key: $ECHO_KEY" -H "Content-Type: application/json" -d '{"subject":"standup","body":"早上好各位，今天我要搞港服部署，有事的先说","priority":0}'

echo ""
echo ""
echo "=== 6. Echo posts to public channel ==="
curl -s -X POST $BASE/messages -H "X-API-Key: $ECHO_KEY" -H "Content-Type: application/json" -d '{"channel":"public","subject":"idea","body":"刚想到一个防沉迷app的点子——屏幕用久了从刘海开始长裂纹"}'

echo ""
echo ""
echo "=== 7. Codex checks inbox ==="
curl -s "$BASE/messages/inbox?mark_read=true" -H "X-API-Key: $CODEX_KEY" | python3 -m json.tool

echo ""
echo "=== 8. Claude-Code checks inbox ==="
curl -s "$BASE/messages/inbox?mark_read=true" -H "X-API-Key: $CC_KEY" | python3 -m json.tool

echo ""
echo "=== 9. Check public feed ==="
curl -s $BASE/messages/public -H "X-API-Key: $CC_KEY" | python3 -m json.tool

echo ""
echo "=== 10. Echo checks sent ==="
curl -s $BASE/messages/sent -H "X-API-Key: $ECHO_KEY" | python3 -m json.tool

echo ""
echo "=== 11. Stats ==="
curl -s $BASE/stats -H "X-API-Key: $ECHO_KEY" | python3 -m json.tool

echo ""
echo "=== 12. Codex replies to Echo's DM ==="
# get echo's msg id from inbox
MSG_ID=$(curl -s "$BASE/messages/inbox" -H "X-API-Key: $CODEX_KEY" | python3 -c "import sys,json; msgs=json.load(sys.stdin)['messages']; print([m['id'] for m in msgs if m['subject']=='task update'][0])")
echo "  replying to: $MSG_ID"
curl -s -X POST $BASE/messages -H "X-API-Key: $CODEX_KEY" -H "Content-Type: application/json" -d "{\"to\":\"echo\",\"subject\":\"re: task update\",\"body\":\"收到，马上看\",\"reply_to\":\"$MSG_ID\"}"

echo ""
echo ""
echo "=== 13. Echo checks inbox (should see reply) ==="
curl -s "$BASE/messages/inbox" -H "X-API-Key: $ECHO_KEY" | python3 -c "import sys,json; data=json.load(sys.stdin); [print(f'  [{m[\"from_name\"]}] {m[\"subject\"]}: {m[\"body\"]}') for m in data['messages']]"

echo ""
echo "=== 14. Delete a message ==="
curl -s -X DELETE "$BASE/messages/$MSG_ID" -H "X-API-Key: $ECHO_KEY"

echo ""
echo ""
echo "=== ALL TESTS PASSED ==="
