# Putting the shops on one machine

One small VM runs the three shops and Caddy, which gives each its HTTPS
hostname. Everything is in `docker-compose.yml`; the secrets are in a `.env`
file that stays on the machine.

```
https://<domain>        a public front page naming the project
https://a.<domain>      Dusk Deals
https://b.<domain>      Praha Pass
https://c.<domain>      Dawn Saver
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

The four hostnames must resolve to the VM's public IP. Without buying a
domain, [DuckDNS](https://www.duckdns.org) gives a free one:

1. Sign in, create a subdomain (say `licensetodeal`) and set its IP to the
   VM's. Subdomains of it (`a.licensetodeal.duckdns.org`) resolve to the same
   IP, which is all that is needed.
2. `DOMAIN=licensetodeal.duckdns.org` in `.env`. With a bought domain as well,
   put the DuckDNS name in `ALT_DOMAIN`, and both keep answering.

With a bought domain: `A` records for the root, `a`, `b` and `c`, to the VM's
IP, and `DOMAIN=yourdomain.com`.

## 3. The code and the secrets

On the VM:

```shell
git clone <the private repository> agent-economy
cd agent-economy/deploy
cp .env.example .env
```

Set `DOMAIN` and `ACME_EMAIL` in `.env`, make up the merchant secret
(`openssl rand -hex 24`) and choose the demo account's password. Type them
on the machine itself: secrets never travel through a chat, a shell history
or the repository.

### Payments

`PAYMENT_RAIL=mock` (the default) simulates every payment and says so on
every order. With Stripe's keys in `.env`, `PAYMENT_RAIL=stripe` switches the
shops to Stripe. Test keys (`sk_test_…`, `pk_test_…`) move no money: in the
shops' checkout the card is `4242 4242 4242 4242` (any date, any CVC;
`4000 0000 0000 0002` is always declined), and every payment, capture and
refund shows up in the Stripe dashboard, in test mode. Live keys would make
the same code move real money; that needs the sponsor's OK first.

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
```

The shops come up with their catalogues and the demo account.

## Day to day

| Task | Command |
|---|---|
| Deploy a change | `git pull && docker compose up -d --build` |
| Deploy a change to the Caddyfile | the same, then `docker compose up -d --force-recreate caddy` (a bind-mounted file keeps its old copy until the container is recreated) |
| Logs of one service | `docker compose logs -f shop-a` (or `shop-b`, `shop-c`, `caddy`) |
| Reset the shops to their catalogues | `RESEED=1 docker compose up -d shop-a shop-b shop-c`, then `docker compose up -d` to drop the flag |
| Change a secret | edit `.env`, then `docker compose up -d` |
| Stop everything | `docker compose down` (data stays in the volumes) |
| Wipe everything | `docker compose down -v` |

Merchant actions (cancel, refund, grant coins, ledger, booking fee) take the
`Simulation-Secret` header with the value from `.env`, exactly as in local
development.

## What is different from running locally

- Agents must sign (`REQUIRE_SIGNATURES=1`) and their profile must be an
  https URL on a public host.
- Cookies are marked Secure, and the shops trust Caddy's forwarded headers so
  the links and photos they build carry the public hostname.
- Data persists across restarts; locally every start re-seeds.
- The ledger of the three shops is one file, in the shared volume.

## Trying the stack on a laptop

`docker-compose.local.yml` runs the same stack without a domain: Caddy signs
its own certificates for `a.localhost`, `b.localhost` and `c.localhost`.

```shell
cp .env.example .env.local      # DOMAIN=localhost, any email, a secret
docker compose -f docker-compose.yml -f docker-compose.local.yml --env-file .env.local up -d --build
curl -k --resolve a.localhost:443:127.0.0.1 https://a.localhost/.well-known/ucp
```

Chrome opens `https://a.localhost` directly (it resolves `*.localhost` itself;
accept the self-signed certificate).
