#!/bin/sh
# サンドボックス内から「自分がどう隔離されているか」を観測するスクリプト
echo "=== OS ==="
cat /etc/os-release 2>/dev/null | head -3

echo "=== PID ==="
cat /proc/self/status 2>/dev/null | grep -E '^(Pid|PPid):'

echo "=== UID ==="
id 2>/dev/null || cat /proc/self/status 2>/dev/null | grep -E '^(Uid|Gid):'

echo "=== Namespace IDs ==="
ls -la /proc/self/ns/ 2>/dev/null | grep -E '(user|mnt|pid)' || echo "(no /proc)"

echo "=== Root filesystem ==="
ls / 2>/dev/null | head -5
echo "..."

echo "=== Network ==="
if command -v curl >/dev/null 2>&1; then
  curl -s --connect-timeout 3 -o /dev/null -w "curl: %{http_code}" https://example.com 2>&1 || echo " (failed)"
elif command -v wget >/dev/null 2>&1; then
  wget -T 3 -q -O /dev/null https://example.com 2>&1 && echo "wget: ok" || echo "wget: failed"
else
  echo "(no curl or wget)"
fi
echo

echo "=== Writable? ==="
touch /tmp/.write_test 2>/dev/null && echo "/tmp: writable" && rm -f /tmp/.write_test || echo "/tmp: read-only"
touch /etc/.write_test 2>/dev/null && echo "/etc: writable" && rm -f /etc/.write_test || echo "/etc: read-only"
