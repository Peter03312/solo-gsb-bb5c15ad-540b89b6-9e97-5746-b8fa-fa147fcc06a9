#!/usr/bin/env bash
# Candidate-set narrowing walkthrough for the Temporary Event Radio
# Spectrum API.
#
# 裁决链（单笔/整组/恢复/候补/预约到期/紧急占用）先在数据库内按时段、覆盖
# 范围和频段缩小候选集，再沿用精确干扰与窗口命中规则。保护带宽只扩张许可
# 干扰范围，不扩张管制窗口；可用频段下界查询覆盖整个候选下界区间；保护带宽
# 变更仍检查全部当前有效许可对。
#
# 本脚本用大量“时段互不相交”的有效许可制造无关记录，演示预演/写入不再逐条
# 装入它们，而真正可能冲突的边界记录一个不漏。
#
# Usage: ./examples/candidate-narrowing.sh [BASE_URL]
set -euo pipefail

BASE="${1:-http://localhost:8000}"
J() { python3 -m json.tool; }
post() { curl -s -X POST "$BASE$1" -H 'Content-Type: application/json' -d "${2:-}"; }
rev() { curl -s "$BASE/api/revision/current" | python3 -c 'import json,sys; print(json.load(sys.stdin)["current_revision"])'; }

N="${N:-100}"
echo "== health =="
curl -s "$BASE/health"; echo

echo "== 1. 装入 $N 个时段互不相交、频段/覆盖范围都很远的有效许可 =="
echo "    （大量无关有效许可：预演/写入不得再逐条装入它们）"
for i in $(seq 1 "$N"); do
  month=$(( (i / 27) % 12 + 1 ))
  day=$(( i % 27 + 1 ))
  m=$(printf "%02d" "$month"); d=$(printf "%02d" "$day")
  ID=$(post /api/applications "{
    \"name\":\"unrelated-$i\",
    \"circle\":{\"cx\":9000,\"cy\":9000,\"radius\":1},
    \"frequency\":{\"low\":9000,\"high\":9001},
    \"time_window\":{\"start\":\"2031-$m-${d}T00:00:00Z\",\"end\":\"2031-$m-${d}T01:00:00Z\"}}" \
    | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
  post "/api/applications/$ID/approve" "{\"expected_revision\":$(rev)}" >/dev/null
done
echo "    当前修订号: $(rev)"

echo
echo "== 2. 一个真正可能冲突的近处许可（频段间隔 1 MHz < 保护带宽 2 MHz） =="
CLOSE_ID=$(post /api/applications '{
  "name":"close",
  "circle":{"cx":0,"cy":0,"radius":10},
  "frequency":{"low":111,"high":112},
  "time_window":{"start":"2031-12-20T08:00:00Z","end":"2031-12-20T10:00:00Z"}}' \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
post "/api/applications/$CLOSE_ID/approve" "{\"expected_revision\":$(rev)}" >/dev/null

echo
echo "== 3. 提交候选并预演：冲突明细只有近处许可，无关许可不装入 =="
CAND=$(post /api/applications '{
  "name":"candidate",
  "circle":{"cx":0,"cy":0,"radius":10},
  "frequency":{"low":100,"high":110},
  "time_window":{"start":"2031-12-20T08:00:00Z","end":"2031-12-20T10:00:00Z"}}' \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
post "/api/applications/$CAND/preview" | python3 -c '
import json,sys
b=json.load(sys.stdin)
print("    冲突申请 id:", [c["application"]["id"] for c in b["conflicts"]])
print("    原因:", [c["reason"] for c in b["conflicts"]])
print("    命中管制窗口:", b["active_restriction_window_ids"])'

echo
echo "== 4. 可用频段下界查询覆盖整个候选下界区间 [90,140] =="
post "/api/applications/$CAND/available-ranges" '{"candidate_low_min":90,"candidate_low_max":140}' \
  | python3 -c '
import json,sys
for r in json.load(sys.stdin)["available_ranges"]:
    lo = "[" if r["min_inclusive"] else "("
    hi = "]" if r["max_inclusive"] else ")"
    print("    %s%s, %s%s" % (lo, r["min"], r["max"], hi))'

echo
echo "== 5. 锁内写入失败：409 interference，申请保持 pending，修订号不变 =="
R=$(rev)
post "/api/applications/$CAND/approve" "{\"expected_revision\":$R}" \
  | python3 -c 'import json,sys; e=json.load(sys.stdin)["error"]; print("    HTTP 语义 -> code:", e["code"], "; 冲突数:", len(e["details"]["conflicts"]))'
curl -s "$BASE/api/applications/$CAND" \
  | python3 -c 'import json,sys; print("    申请状态:", json.load(sys.stdin)["status"])'
echo "    修订号仍是: $(rev)"

echo
echo "== 6. 管制窗口预演：保护带宽不扩张窗口，1 MHz 间隔不命中 =="
post /api/restriction-windows/preview '{
  "circle":{"cx":0,"cy":0,"radius":10},
  "frequency":{"low":113,"high":120},
  "time_window":{"start":"2031-12-20T08:00:00Z","end":"2031-12-20T11:00:00Z"},
  "reason":"gap never hits"}' \
  | python3 -c 'import json,sys; print("    间隔 1 MHz 的窗口命中数:", json.load(sys.stdin)["hit_count"])'
post /api/restriction-windows/preview '{
  "circle":{"cx":0,"cy":0,"radius":10},
  "frequency":{"low":110,"high":112},
  "time_window":{"start":"2031-12-20T09:00:00Z","end":"2031-12-20T11:00:00Z"},
  "reason":"closed-band touch hits"}' \
  | python3 -c 'import json,sys; b=json.load(sys.stdin); print("    闭区间端点接触的命中:", b["hit_effective_application_ids"], "; 命中数:", b["hit_count"])'

echo
echo "== 7. 保护带宽变更仍检查全部当前有效许可对（不收窄） =="
# 再加一对间隔恰为 2 MHz 的许可（当前 2 MHz 允许；提议 3 MHz 即冲突）。
P1=$(post /api/applications '{
  "name":"pair-low",
  "circle":{"cx":0,"cy":0,"radius":10},
  "frequency":{"low":200,"high":201},
  "time_window":{"start":"2031-12-21T08:00:00Z","end":"2031-12-21T10:00:00Z"}}' \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
post "/api/applications/$P1/approve" "{\"expected_revision\":$(rev)}" >/dev/null
P2=$(post /api/applications '{
  "name":"pair-high",
  "circle":{"cx":0,"cy":0,"radius":10},
  "frequency":{"low":203,"high":204},
  "time_window":{"start":"2031-12-21T08:00:00Z","end":"2031-12-21T10:00:00Z"}}' \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
post "/api/applications/$P2/approve" "{\"expected_revision\":$(rev)}" >/dev/null
post /api/config/guard-band/preview '{"guard_band":3.0}' \
  | PAIR="$P1,$P2" NCOUNT="$N" python3 -c '
import json,sys,os
b=json.load(sys.stdin)
n = int(os.environ["NCOUNT"])
pair = tuple(int(x) for x in os.environ["PAIR"].split(","))
pairs = {frozenset((p["first"]["id"], p["second"]["id"])) for p in b["interfering_pairs"]}
print("    受检有效许可数:", len(b["effective_application_ids"]),
      "（=", n + 3, "，含全部时段不相交许可，未做候选收窄）")
print("    3 MHz 带宽下干扰对数:", b["interfering_pair_count"],
      "; 2 MHz 间隔对被检出:", frozenset(pair) in pairs)'

echo
echo "== 8. 候补链路：撤销近处许可后，候选在同一事务内自动获批 =="
post "/api/applications/$CAND/waitlist" '{"reason":"band busy"}' >/dev/null
post "/api/applications/$CLOSE_ID/revoke" "{\"expected_revision\":$(rev),\"reason\":\"release\"}" \
  | python3 -c '
import json,sys
b=json.load(sys.stdin)
print("    自动批准 id:", b["auto_approved_application_ids"])
print("    剩余候补:", b["remaining_waitlist_application_ids"])
print("    最终修订号:", b["final_revision"])'

echo
echo "== 完成：服务全程可用；无关许可不参与任何裁决链的候选集 =="
