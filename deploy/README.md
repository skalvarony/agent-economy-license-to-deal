# Putting the shops, the agents, the console and the venue on one machine

One small VM runs the three shops, one agent per person, the shops' console,
the venue simulator and Caddy, which gives each its HTTPS hostname. Everything is in `docker-compose.yml`; the
secrets are in a `.env` file that stays on the machine.

```
https://<domain>        a public front page naming the project
https://a.<domain>      Dusk Deals
https://b.<domain>      Praha Pass
https://c.<domain>      Dawn Saver
https://<agent host>    one agent per person, on its own name; sign-in page
https://admin.<domain>  the shops' console; sign-in page, team account
https://venue.<domain>  the venue simulator; sign-in page, team account
```

## 1. A machine

Any Linux VM with 2 vCPU and 2 to 4 GB of RAM. Hetzner Cloud's smallest shared
VM in Falkenstein or Nuremberg costs about 4 € a month and is close to Prague.
Ubuntu 24.04. Open only ports 22, 80 and 443 in its firewall.

Install Docker on it (as root, or with `sudo`):

```shell
curl -fsSL https://get.docker.com | sh
```

## 2. Names

Two kinds: the shops' domain, and one name per agent, apart from it.

The six hostnames must resolve to the VM's public IP. Without buying a
domain, [DuckDNS](https://www.duckdns.org) gives a free one:

1. Sign in, create a subdomain (say `licensetodeal`) and set its IP to the
   VM's. Subdomains of it (`a.licensetodeal.duckdns.org`) resolve to the same
   IP, which is all that is needed.
2. `DOMAIN=licensetodeal.duckdns.org` in `.env`. With a bought domain as well,
   put the DuckDNS name in `ALT_DOMAIN`, and both keep answering.

With a bought domain: `A` records for the root, `a`, `b`, `c`, `admin` and
`venue`, to the VM's IP, and `DOMAIN=yourdomain.com`.

Each agent gets its own name (free DuckDNS names do: `ltd-agent-alvaro`,
`ltd-agent-david`, `ltd-agent-emmanouil`), pointed at the same IP and set
as `AGENT_<NAME>_HOST` in `.env`. The shops then verify a key published on a
domain they don't control.

## 3. The code and the secrets

On the VM:

```shell
git clone <the private repository> agent-economy
cd agent-economy/deploy
cp .env.example .env
```

Set `DOMAIN`, the agents' hosts and `ACME_EMAIL` in `.env`, then the
secrets, typed on the machine itself (they never travel through a chat, a
shell history or the repository):

- `SIMULATION_SECRET`: `openssl rand -hex 24`.
- `DEMO_PASSWORD`: the demo account's password in every shop.
- The team's sign-in for the console, the venue and the agents' pages: `AGENT_USER` and
  `AGENT_PASSWORD_HASH`, the hash from
  `docker run --rm caddy:2-alpine caddy hash-password --plaintext '…'`,
  with every `$` written as `$$`, as Compose needs. The sign-in is the app's
  own (`shared/teamlogin.py`, copied into the console, venue and agent images): a
  page at `/login`, a session cookie for a week, `/logout`. The team account
  opens the console and the venue; each agent opens with its
  person's own login if one is given in `LOGIN_USERS_<NAME>` ("user:hash"),
  else with the team's. An agent's `/profile.json` and `/mcp/<key>` stay
  open: shops fetch the one, the person's ChatGPT the other.
- `OPENAI_API_KEY` is optional: without it the scripted stand-in decides for
  the agent. `AGENT_MODEL` names the model.

### Telegram

Each agent may have a Telegram bot: in Telegram, talk to **@BotFather**,
`/newbot`, give it a name (say "Alvaro's shopping agent") and a username
ending in `bot`; it answers with a token. Put it in `.env` as
`TELEGRAM_BOT_TOKEN_<NAME>`; `docker compose up -d` after. Then, on the
agent's page, **What it knows about you → Connections → Link Telegram**
gives a one-time link that pairs the person's chat with the agent; only that
chat can talk to it afterwards. Proposals from any channel arrive there as
cards with Approve / Not this one, and a tap is recorded as
`method: telegram`.

### Payments

`PAYMENT_RAIL=mock` (the default) simulates every payment and says so on
every order. With Stripe's keys, `PAYMENT_RAIL=stripe` switches the shops to
Stripe and the two `AGENT_CARD_*` lines move the agents to Stripe's test
card (`AGENT_CARD_HANDLER=stripe`, `AGENT_CARD_TOKEN=pm_card_visa`). Test
keys (`sk_test_…`, `pk_test_…`) move no money: in the shops' checkout the
card is `4242 4242 4242 4242` (any date, any CVC; `4000 0000 0000 0002` is
always declined), and every payment, capture and refund shows up in the
Stripe dashboard, in test mode. Live keys would make the same code move real
money; that needs the sponsor's OK first.

## 4. Up

```shell
docker compose up -d --build
docker compose ps
docker compose logs -f
```

Caddy gets the certificates on the first request to each hostname; that can
take a few seconds. Then:

```shell
curl https://a.<domain>/.well-known/ucp
curl https://<agent host>/profile.json
```

The shops come up with their catalogues and the demo account. The agent makes
its key on the first start and keeps it in its volume, so it stays the same
agent across restarts.

## Day to day

| Task | Command |
|---|---|
| Deploy a change | `git pull && docker compose up -d --build` |
| Deploy a change to the Caddyfile | the same, then `docker compose up -d --force-recreate caddy` (a bind-mounted file keeps its old copy until the container is recreated) |
| Logs of one service | `docker compose logs -f shop-a` (or `shop-b`, `shop-c`, `agent-alvaro`, `agent-david`, `agent-emmanouil`, `console`, `venue`, `caddy`) |
| Change who may sign in | edit `.env`, then `docker compose up -d` (sessions already open stay valid until they expire) |
| Reset the shops to their catalogues | `RESEED=1 docker compose up -d shop-a shop-b shop-c`, then `docker compose up -d` to drop the flag |
| Change a secret | edit `.env`, then `docker compose up -d` |
| Stop everything | `docker compose down` (data stays in the volumes) |
| Wipe everything | `docker compose down -v` |

Merchant actions (cancel, refund, grant coins, ledger, booking fee) take the
`Simulation-Secret` header with the value from `.env`, exactly as in local
development. The agent's page asks for a sign-in; its `/profile.json` is
public because the shops fetch it.

## What is different from running locally

- Agents must sign (`REQUIRE_SIGNATURES=1`) and their profile must be an
  https URL on a public host. The agent's is.
- Cookies are marked Secure, and the shops trust Caddy's forwarded headers so
  the links and photos they build carry the public hostname.
- Data persists across restarts; locally every start re-seeds.
- The ledger of the three shops is one file, in the shared volume.

## Trying the stack on a laptop

`docker-compose.local.yml` runs the same stack without a domain: Caddy signs
its own certificates for `a.localhost`, `b.localhost`, `c.localhost` and the
agents' names, and the agents and the shops talk to each other by container
name.

```shell
cp .env.example .env.local      # DOMAIN=localhost, agent hosts *.localhost, any email, a secret, a hash
docker compose -f docker-compose.yml -f docker-compose.local.yml --env-file .env.local up -d --build
curl -k --resolve a.localhost:443:127.0.0.1 https://a.localhost/.well-known/ucp
```

Chrome opens `https://a.localhost` directly (it resolves `*.localhost` itself;
accept the self-signed certificate).
