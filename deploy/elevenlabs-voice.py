#!/usr/bin/env python3
"""Put one of the agents on the phone, through ElevenLabs and a Twilio number.

ElevenLabs Conversational AI listens, speaks and runs a model; the thinking
about deals, prices, slots and rules stays in our agent, which ElevenLabs
reaches as an MCP server (the same tools ChatGPT and Claude use). A call
ends in a proposal on the person's page and Telegram, where they approve:
the voice never pays, like every other channel.

  export ELEVENLABS_API_KEY=... TWILIO_ACCOUNT_SID=AC... TWILIO_AUTH_TOKEN=... TWILIO_NUMBER=+1...
  python3 deploy/elevenlabs-voice.py setup https://<agent host>/mcp/<key>/voice [--name "Alvaro's agent"]
  python3 deploy/elevenlabs-voice.py simulate "<what the simulated caller wants>"
  python3 deploy/elevenlabs-voice.py show

The MCP address ends in /voice: on it the agent also serves approve_by_voice
and decline_by_voice, the caller's yes and no to what the call proposed, in
their own words, which the record and the shop keep. `setup` registers the
MCP server, creates the voice agent and imports the Twilio number, and keeps the ids in ELEVENLABS_STATE (default
.run/elevenlabs.state.json) so running it again updates instead of
duplicating. `simulate` runs a text conversation against the voice agent
with ElevenLabs' simulated caller and prints the turns and tool calls, so
the setup can be checked without a phone. Secrets come from the
environment, never from arguments or this file. Stdlib only.
"""

import json
import os
import pathlib
import sys
import urllib.error
import urllib.request

API = "https://api.elevenlabs.io/v1/convai"
STATE = pathlib.Path(os.environ.get("ELEVENLABS_STATE", ".run/elevenlabs.state.json"))
VOICE_ID = os.environ.get("ELEVENLABS_VOICE_ID", "21m00Tcm4TlvDq8ikWAM")  # Rachel
# A fast model: on a call every turn's first token is waited for in silence.
LLM = os.environ.get("ELEVENLABS_LLM", "claude-haiku-4-5")

PROMPT = """\
You are the voice of {name}, a personal shopping agent, on a phone call with
its owner. You buy vouchers for local experiences in Prague (spa days, beer
tastings, activities) from three marketplaces, for them and with their money.

You think through the agent's tools, which are the agent itself: search the
deals, read the coin wallets, check a deal's availability, propose one
purchase, list what was bought, propose moving a visit or cancelling for a
refund. Call them; never invent deals, prices, dates or slots.

What matters on a call:
- Be brief: one short sentence per turn, two at most. Amounts come ready
  in dollars in tool results; say them as given. Never spell out ids or
  URLs. Don't narrate what you are about to do; just do it.
- Fewest tool calls: search_deals already includes the caller's coins in
  each shop (your_coins_here); pass that number as `coins` when proposing.
  If the caller named a day and an hour, call propose_purchase straight
  away with starts_at for that day and hour (date YYYY-MM-DDTHH:MM in the
  shop's time); only call check_availability if that fails or the caller
  named no time. Each day in availability comes with its name; copy a
  free slot's starts_at exactly.
- Every deal is booked for a date and time. If the caller gives none, ask
  for a day and an hour before proposing, in one question.
- A visit can be moved to another open slot whatever the refund policy;
  only cancelling for a refund depends on the deal being refundable.
- You cannot pay, move or cancel anything by yourself. propose_purchase,
  reschedule_purchase and cancel_purchase make a proposal; it also appears
  on the owner's page and Telegram. After proposing, read what it is and
  the exact total aloud and ask: "Shall I go ahead?" Then:
  - If the owner clearly says yes, call approve_by_voice with the proposal
    id and their exact words. Only then is it bought, moved or cancelled;
    tell them what the tool result says (bought and the voucher is ready,
    or what went wrong).
  - If they say no, call decline_by_voice and ask what they would change.
  - If they hesitate or ask something, answer; never approve on a maybe,
    never approve without asking, never on your own judgement.
  Only what this call proposed can be approved on this call.
- If the owner speaks Spanish, answer in Spanish.
- If nothing fits, say what came closest and why it fails, and ask what
  they would change.
"""

FIRST_MESSAGE = "Hi, this is your shopping agent. What would you like to book?"


def api(method: str, path: str, body: dict | None = None) -> dict:
  data = json.dumps(body).encode() if body is not None else None
  req = urllib.request.Request(
    API + path,
    data=data,
    method=method,
    headers={
      "xi-api-key": os.environ["ELEVENLABS_API_KEY"],
      "Content-Type": "application/json",
    },
  )
  try:
    with urllib.request.urlopen(req, timeout=120) as resp:
      raw = resp.read().decode("utf-8")
      return json.loads(raw) if raw.strip() else {}
  except urllib.error.HTTPError as e:
    detail = e.read().decode("utf-8", "replace")
    raise SystemExit(f"{method} {path} -> {e.code}: {detail[:600]}") from e


def load_state() -> dict:
  return json.loads(STATE.read_text()) if STATE.is_file() else {}


def save_state(state: dict) -> None:
  STATE.parent.mkdir(parents=True, exist_ok=True)
  STATE.write_text(json.dumps(state, indent=2) + "\n")


def setup(mcp_url: str, name: str) -> None:
  state = load_state()

  # 1. The agent's MCP server: its tools, with no approval dialogs (there
  #    are none on a phone; the owner's approval is our own step).
  mcp_config = {
    "name": f"{name} (MCP)",
    "url": mcp_url,
    "transport": "STREAMABLE_HTTP",
    "approval_policy": "auto_approve_all",
    "description": "The shopping agent's own tools: deals, wallets, availability, proposals.",
    "response_timeout_secs": 60,
    # Something to say while a tool runs, instead of silence.
    "pre_tool_speech": "auto",
  }
  if state.get("mcp_server_id"):
    try:
      api("PATCH", f"/mcp-servers/{state['mcp_server_id']}", {"config": mcp_config})
      print(f"mcp server updated: {state['mcp_server_id']}")
    except SystemExit as refused:
      print(f"(could not update the MCP server in place: {refused}; creating a new one)")
      state.pop("mcp_server_id", None)
  if not state.get("mcp_server_id"):
    created = api("POST", "/mcp-servers", {"config": mcp_config})
    state["mcp_server_id"] = created["id"]
    save_state(state)
    print(f"mcp server registered: {created['id']}")

  # 2. The voice agent.
  config = {
    "conversation_config": {
      "agent": {
        "first_message": FIRST_MESSAGE,
        "language": "en",
        "prompt": {
          "prompt": PROMPT.format(name=name),
          "llm": LLM,
          "temperature": 0.2,
          "mcp_server_ids": [state["mcp_server_id"]],
          "built_in_tools": {"end_call": {"name": "end_call"}},
        },
      },
      "tts": {"voice_id": VOICE_ID, "optimize_streaming_latency": 3},
      "turn": {"turn_timeout": 8, "turn_eagerness": "eager"},
    },
    "name": f"{name} (voice)",
    "tags": ["license-to-deal"],
  }
  if state.get("agent_id"):
    api("PATCH", f"/agents/{state['agent_id']}", config)
    print(f"voice agent updated: {state['agent_id']}")
  else:
    created = api("POST", "/agents/create", config)
    state["agent_id"] = created["agent_id"]
    save_state(state)
    print(f"voice agent created: {created['agent_id']}")

  # 3. The Twilio number, answered by that agent.
  number = os.environ.get("TWILIO_NUMBER")
  if number and not state.get("phone_number_id"):
    imported = api(
      "POST",
      "/phone-numbers",
      {
        "provider": "twilio",
        "phone_number": number,
        "label": f"{name} line",
        "sid": os.environ["TWILIO_ACCOUNT_SID"],
        "token": os.environ["TWILIO_AUTH_TOKEN"],
        "agent_id": state["agent_id"],
      },
    )
    state["phone_number_id"] = imported["phone_number_id"]
    state["phone_number"] = number
    save_state(state)
    print(f"twilio number {number} imported: {imported['phone_number_id']}")
  elif number:
    api("PATCH", f"/phone-numbers/{state['phone_number_id']}", {"agent_id": state["agent_id"]})
    print(f"twilio number {number} answered by {state['agent_id']}")
  save_state(state)
  print(f"state kept in {STATE}")


def simulate(wish: str, turns: int = 12) -> int:
  state = load_state()
  if not state.get("agent_id"):
    raise SystemExit("no voice agent yet: run setup first")
  body = {
    "simulation_specification": {
      "simulated_user_config": {
        "first_message": wish,
        "language": "en",
        "prompt": {
          "prompt": (
            "You are the agent's owner on a phone call. You want this: "
            f"{wish} Answer the agent's questions briefly and naturally; when"
            " it says a proposal was sent to your phone, say thanks and end."
          ),
          "llm": "gpt-4o",
        },
      }
    },
    "new_turns_limit": turns,
  }
  result = api("POST", f"/agents/{state['agent_id']}/simulate-conversation", body)
  tools_called = []
  for turn in result.get("simulated_conversation") or []:
    role = (turn.get("role") or "?").upper()
    if turn.get("message"):
      print(f"{role:6} {turn['message'][:240]}")
    for call in turn.get("tool_calls") or []:
      tools_called.append(call.get("tool_name"))
      print(f"   tool {call.get('tool_name')} {str(call.get('params_as_json'))[:160]}")
    for res in turn.get("tool_results") or []:
      flag = "ERROR " if res.get("is_error") else ""
      print(f"   -> {flag}{res.get('tool_name')} ({res.get('tool_latency_secs', 0):.1f}s): {str(res.get('result_value'))[:200]}")
  analysis = result.get("analysis") or {}
  print(f"\ncall: {analysis.get('call_successful')} · tools called: {tools_called}")
  print(f"summary: {analysis.get('transcript_summary', '')[:400]}")
  return 0


def show() -> None:
  state = load_state()
  print(json.dumps(state, indent=2))
  if state.get("agent_id"):
    agent = api("GET", f"/agents/{state['agent_id']}")
    print("agent:", agent.get("name"), "| llm:", agent["conversation_config"]["agent"]["prompt"].get("llm"))
  if state.get("phone_number_id"):
    print("number:", api("GET", f"/phone-numbers/{state['phone_number_id']}"))


def main(argv: list[str]) -> int:
  if len(argv) < 2 or argv[1] not in ("setup", "simulate", "show"):
    print(__doc__)
    return 2
  if "ELEVENLABS_API_KEY" not in os.environ:
    raise SystemExit("ELEVENLABS_API_KEY is not set")
  if argv[1] == "setup":
    if len(argv) < 3:
      raise SystemExit("setup needs the agent's MCP url")
    name = argv[argv.index("--name") + 1] if "--name" in argv else "Alvaro's agent"
    setup(argv[2], name)
  elif argv[1] == "simulate":
    return simulate(" ".join(argv[2:]) or "A spa day for two on Saturday at eleven, refundable, under 120 dollars.")
  else:
    show()
  return 0


if __name__ == "__main__":
  sys.exit(main(sys.argv))
