"""Who is at the web door: a person, or a program that says so.

The shop doesn't try to guess whether a browser is driven by a person or by
an AI agent; it can't, and guessing badly annoys people. It does two cheaper
things:

- It recognises agents that declare themselves, by the names the big AI
  providers publish for their agents and crawlers, or by signing their
  requests (Web Bot Auth, the same signature standard UCP uses). A declared
  agent may browse, but is sent to the agent door to buy: there it is
  identified and the customer's approval can be checked.
- It notes hints that the visitor is a program (a headless browser, an HTTP
  client, a form sent without the page's script) and keeps them on the order.
  Hints are not proof; they are there for whoever decides a complaint later.
"""

import dataclasses
import re
import time
from collections.abc import Mapping
from typing import Any

# How declared agents name themselves in the User-Agent header. Each provider
# publishes these; the list is the public one, not a guess.
DECLARED_AGENTS: list[tuple[str, str]] = [
  (r"ChatGPT-User", "OpenAI ChatGPT"),
  (r"OAI-SearchBot|GPTBot", "OpenAI crawler"),
  (r"Claude-User|Claude-SearchBot|ClaudeBot|anthropic-ai", "Anthropic Claude"),
  (r"Perplexity-User|PerplexityBot", "Perplexity"),
  (r"GoogleAgent-Mariner", "Google agent"),
  (r"Google-Extended", "Google crawler"),
  (r"MistralAI-User", "Mistral"),
  (r"DuckAssistBot", "DuckDuckGo assistant"),
  (r"meta-externalagent", "Meta agent"),
  (r"Amazonbot|Applebot|bingbot|Googlebot", "a search crawler"),
]
# Hints that a program, not a person at a browser, sent the request.
HINTS: list[tuple[str, str]] = [
  (r"HeadlessChrome|Headless", "headless browser"),
  (r"Playwright|Puppeteer|Selenium|WebDriver", "browser automation"),
  (
    r"python-requests|python-httpx|aiohttp|curl/|Wget|Go-http-client"
    r"|node-fetch|undici|axios|okhttp|Java/|libwww|PostmanRuntime",
    "HTTP client, not a browser",
  ),
]


@dataclasses.dataclass
class Visitor:
  """What the request says about who sent it."""

  # The declared agent's name, when the request says it comes from one.
  agent: str | None = None
  # How it declared itself: "user-agent" or "web-bot-auth".
  declared_by: str | None = None
  # Hints that a program sent it. Not proof.
  hints: list[str] = dataclasses.field(default_factory=list)

  @property
  def declared(self) -> bool:
    """Whether the visitor says it is an agent."""
    return self.agent is not None

  def as_dict(self) -> dict[str, Any]:
    """Return the visitor as it is kept on an order."""
    return dataclasses.asdict(self)


def classify(headers: Mapping[str, str]) -> Visitor:
  """Read who a web request says it is from its headers."""
  user_agent = headers.get("user-agent", "")
  visitor = Visitor()
  # Web Bot Auth: the agent signs its page requests and names the directory
  # of its keys. The shop doesn't verify the signature here; it only takes
  # the declaration at its word, which is all a declaration needs.
  directory = headers.get("signature-agent")
  if directory or "signature-input" in headers:
    visitor.agent = _host(directory) or "a signing agent"
    visitor.declared_by = "web-bot-auth"
  for pattern, name in DECLARED_AGENTS:
    if re.search(pattern, user_agent, re.I):
      visitor.agent = visitor.agent or name
      visitor.declared_by = visitor.declared_by or "user-agent"
      break
  for pattern, hint in HINTS:
    if re.search(pattern, user_agent, re.I):
      visitor.hints.append(hint)
  if user_agent and "Mozilla/" not in user_agent and not visitor.hints:
    visitor.hints.append("not a browser")
  if not user_agent:
    visitor.hints.append("no User-Agent")
  return visitor


def _host(directory: str | None) -> str | None:
  if not directory:
    return None
  found = re.search(r"[\w.-]+\.\w+", directory.strip('"'))
  return found.group(0) if found else None


def purchase_record(
  visitor: Visitor, *, page_script: bool, shown_at: str | None
) -> dict[str, Any]:
  """Describe a web purchase for the order: who, and how it was sent.

  `page_script` says whether the page's own script sent the payment (a plain
  form post means no script ran: a person with scripts off, or a program).
  `shown_at` is when the checkout page was rendered, from its hidden field.
  """
  record = visitor.as_dict()
  record["page_script"] = page_script
  hints = list(record["hints"])
  if not page_script:
    hints.append("payment sent without the page's script")
  try:
    seconds = round(time.time() - float(shown_at or ""))
  except ValueError:
    seconds = None
  record["seconds_on_checkout"] = seconds
  if seconds is not None and seconds < 2:
    hints.append("paid within two seconds of seeing the page")
  record["hints"] = hints
  return record


def describe(record: dict[str, Any] | None) -> str:
  """Say in words what a web order's record tells about its buyer."""
  if not record:
    return ""
  parts = []
  if record.get("agent"):
    parts.append(f"declared as {record['agent']}")
  if record.get("hints"):
    parts.append(", ".join(record["hints"]))
  return "; ".join(parts)
