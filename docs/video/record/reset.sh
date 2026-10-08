#!/usr/bin/env bash
# Put the LOCAL stack in a clean state for recording the demo film.
# Never run against the server: it only touches .run/ on this machine.
#
#   docs/video/record/reset.sh
#
# - stops the agent and deletes its local history (thread, approvals, proposals)
# - moves the venue's local log aside (so the arrivals table starts empty)
# - re-seeds the three shops with signature checks on (the server's default),
#   so the shops verify the agent's signature exactly as in production
# - starts the agent and waits until it answers
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
cd "$ROOT"

scripts/agent.sh stop
rm -rf .run/agent/journal.jsonl .run/agent/approvals.jsonl .run/agent/proposals.json \
  .run/agent/pending.json .run/agent/conversations/
if [[ -f .run/venue/happened.jsonl ]]; then
  mv .run/venue/happened.jsonl ".run/venue/happened.$(date +%Y%m%d-%H%M%S).jsonl"
fi
scripts/shops.sh stop
REQUIRE_SIGNATURES=1 scripts/shops.sh start
scripts/agent.sh start

for _ in $(seq 1 60); do
  if curl -sf -o /dev/null http://localhost:8190/api/state; then
    for port in 8181 8182 8183; do
      until curl -sf -o /dev/null "http://localhost:$port/.well-known/ucp"; do sleep 0.5; done
    done
    echo "clean: agent and shops up"
    exit 0
  fi
  sleep 1
done
echo "agent did not come up; see .run/agent/server.log" >&2
exit 1
