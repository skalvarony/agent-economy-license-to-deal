// Short guides, one per thing the person may need to set up or understand.
// Shown in a panel from "How to set it up" links; also at /guides/<name>.
window.GUIDES = {
  telegram: {
    title: "Telegram",
    lead: "Your agent on your phone: ask there, approve there. One bot per agent, and only your chat can talk to it.",
    steps: [
      ["Create the bot (once, by the team)", "In Telegram, open @BotFather, send /newbot, give it a name and a username ending in “bot”. It answers with a token. The token goes into the server's .env as TELEGRAM_BOT_TOKEN_<NAME> through deploy/set-secrets.sh, then docker compose up -d. It never goes through a chat."],
      ["Link your chat", "Here, in Connections → Telegram, press “Link Telegram”, then “Open in Telegram” and Start. The link works once and for 15 minutes. The row then says “Linked”."],
      ["Ask", "Write to the bot as you would here: “a spa day for two, refundable, under $100”. The agent searches the shops and answers."],
      ["Approve", "Every proposal, from any channel (this page, ChatGPT, Claude or Telegram itself), arrives as a card with photo, terms, the total and the buttons Approve / Not this one / Open in my agent. A tap is your decision; the card is edited with the outcome and a receipt with the voucher codes follows a purchase."],
    ],
    notes: [
      "The record says the yes came “by telegram”. History shows it with Telegram's mark.",
      "/pending re-sends what waits for your approval. /help explains the bot.",
      "Unlink here at any time; the bot then refuses every chat until a new link.",
    ],
    trouble: [
      ["The row says “Not set up”", "The agent has no token. The team runs set-secrets.sh on the server and restarts."],
      ["The bot answers “This agent belongs to someone else”", "Your chat isn't the linked one. Unlink here and link again from the chat you want."],
      ["No card arrives", "Check the agent's page: Approvals shows what waits. If the proposal is there and the card isn't, the bot may have been restarted; ask it /pending."],
    ],
  },
  chatgpt: {
    title: "ChatGPT",
    lead: "Your ChatGPT uses this agent as its tools and shows the proposal as a card inside the chat, with Approve on it.",
    steps: [
      ["Copy the MCP address", "Connections → MCP address → Copy. It contains your key: don't paste it anywhere public."],
      ["Add the connector", "ChatGPT → Settings → Apps & Connectors → Advanced → Developer mode on → Create. Name it, paste the address as the MCP server URL, authentication “No authentication”, tick “I trust this application”, Create."],
      ["Ask", "In a new chat with the connector enabled: “Using my shopping agent, propose the cheapest refundable spa for two in Prague on October 10.”"],
      ["Approve on the card", "The proposal appears as a card: photo, terms, total, Approve / Not this one / Open in my agent. Approving happens on the card (it is this agent's own interface, embedded), never by the model: there is no tool that approves or pays."],
    ],
    notes: [
      "The record says the yes came “by card:ChatGPT”.",
      "If ChatGPT refuses to propose (“a safety check blocked it”), ask again saying you will approve yourself; it is ChatGPT's own purchase guard.",
      "After a change on the agent, press Refresh on the connector, or remove and re-create it: ChatGPT caches the tool list.",
    ],
    trouble: [
      ["The card doesn't show", "Refresh the connector; ChatGPT keeps the old card. Then ask for a new proposal (old cards are not redrawn)."],
      ["Approve seems to do nothing", "The card asks the agent how the proposal stands; wait a few seconds. If the shop refused (a purchase limit, say), the card says so."],
    ],
  },
  claude: {
    title: "Claude",
    lead: "Claude (web and desktop) draws the same card as ChatGPT. Claude Code, a terminal, gets a link to Approvals instead.",
    steps: [
      ["Add the connector", "Claude → Settings → Connectors → Add custom connector. Name it, paste the MCP address, “No sign-in”, Streamable HTTP, Add."],
      ["Ask", "Enable the connector in a chat and ask for a proposal. The card appears below Claude's answer."],
      ["Approve on the card", "Approve / Not this one on the card; “Open in my agent” leads here. The record says “by card:Claude” or “card:Claude Desktop”."],
      ["Claude Code", "claude mcp add --transport http ltd-agent <MCP address>. It proposes and hands you the link to Approvals; it cannot approve."],
    ],
    notes: [
      "Claude requires the agent to speak a recent protocol version and declare the UI extension; the agent does both.",
      "If Claude hides the card's buttons or asks permission for approve_from_card, allow it: it is your tap.",
    ],
    trouble: [
      ["Claude says it couldn't open the checkout for consent reasons", "Claude's purchase guard. Ask again saying you'll approve on the card."],
    ],
  },
  approvals: {
    title: "Approvals",
    lead: "Nothing is paid until you approve the exact total, on an interface this agent owns.",
    steps: [
      ["Where proposals wait", "Approvals lists every proposal still waiting, wherever it was made: this chat, ChatGPT, Claude, Telegram. Each says who proposed it."],
      ["How you approve", "On this page, on the card inside ChatGPT or Claude, or with a tap in Telegram. Never by the model: no brain has a tool that approves or pays."],
      ["What gets recorded", "Who, when, which proposal, the total you saw, and the channel (page, card:ChatGPT, card:Claude, telegram). Evidence lays the record next to the shop's order."],
      ["If the shop changes the total", "The purchase stops and a new proposal with the new total waits for you. Nothing is charged at a total you didn't see."],
    ],
    notes: ["Waiting proposals survive a restart of the agent; the thread on the page keeps every channel's messages, in order."],
    trouble: [],
  },
  rules: {
    title: "Hard rules",
    lead: "Limits only you set, enforced by the app in code, not by the model.",
    steps: [
      ["Never above", "A proposal above this card total is refused before any checkout opens."],
      ["Refundable only", "Final-sale deals are never proposed."],
      ["Never these categories", "Deals in them are never proposed, even if you ask."],
    ],
    notes: ["Preferences (“What it has learnt”) are different: the agent may learn them from what you say and weighs them; rules it cannot cross."],
    trouble: [],
  },
};
