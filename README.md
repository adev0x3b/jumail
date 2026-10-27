# JuMail

Send-only SMTP relay for every app under **juume.site** (JuLinks, JuPDF, JuDesk,
Household, NovaCrm). Apps get one SMTP endpoint and one login each; JuMail
authenticates them, DKIM-signs the message, queues it and hands it to a
transactional provider (Brevo's free tier by default) that delivers it from
reputable IPs.

It exists because a home connection cannot deliver mail directly: ISPs block
port 25 outbound and residential IPs sit on the Spamhaus PBL, so Gmail and
Outlook reject them outright. The relay keeps the parts worth owning (identity,
credentials, signing, policy, queue) at home and rents only the last hop.

```
 JuLinks ─┐                                      ┌────────────────────┐
 JuPDF  ──┤  587 STARTTLS, per-app login        │ JuMail (maddy)     │  587 STARTTLS + AUTH   ┌───────┐      ┌───────┐
 JuDesk ──┼──────────────────────────────────▶   │ auth · allow-list  │ ──────────────────────▶│ Brevo │ ───▶ │ inbox │
 Rails  ──┘  docker network "jumail"            │ DKIM · rate · queue│                        └───────┘      └───────┘
                                                 └────────────────────┘
                                                 no port 25, no IMAP, nothing reachable from the internet
```

Engine: [maddy](https://maddy.email) 0.9, a single Go binary configured by one
file. No web UI, no database server, no mailboxes.

## Security model

- **Nothing listens on the internet.** The only listener is submission on 587,
  reachable from the `jumail` docker network and from `127.0.0.1` on the host.
  There is no port 25, so an open relay is impossible by construction.
- **Authentication is mandatory and never in clear text.** `insecure_auth no`
  means AUTH is not even advertised before STARTTLS. TLS 1.2+ only.
- **One login per app, bcrypt-hashed** in an SQLite table inside the data
  volume. Rotating or revoking one app never touches the others.
- **An app can only send as itself.** `config/senders` maps each login to the
  From addresses it may use; envelope *and* header From are checked, so
  JuPDF cannot send as `julinks@juume.site`, and nobody can send as the CEO.
- **Only juume.site senders are accepted**; everything else gets a 501.
- **Global rate limit** (`JUMAIL_RATE_BURST`/`PERIOD`, default 30 per minute)
  protects the provider quota and limits blast radius if one app is
  compromised. Burst beyond it is deferred with 451, not dropped.
- **DKIM-signed at the relay** with a 2048-bit RSA key that never leaves the
  volume, selector `jumail`. SPF and DMARC records complete the picture.
- **Container hardening:** non-root user, read-only root filesystem, all
  capabilities dropped, `no-new-privileges`, 10 MB message cap, log rotation.
- **Secrets live in `.env` only** (provider key, Cloudflare token). The repo
  can be public.

## Quick start (docker host)

```bash
git clone git@github.com:adev0x3b/jumail.git && cd jumail
./jumail init                 # creates .env, fill in the blanks
./jumail up                   # starts the relay (ACME fetches the certificate)
./jumail dns                  # SPF, DKIM, DMARC records to paste into Cloudflare
./jumail user add julinks julinks@juume.site noreply@juume.site
./jumail test you@gmail.com noreply@juume.site julinks
```

`.env` needs:

| key | what |
|---|---|
| `RELAY_USER`, `RELAY_PASSWORD` | Brevo login (account email) and an SMTP key from *SMTP & API* |
| `ACME_EMAIL`, `CLOUDFLARE_API_TOKEN` | Let's Encrypt contact and a token limited to *Zone › DNS › Edit* on juume.site |

Before the first mail: verify the domain in Brevo (it asks for its own TXT
records), then publish the three records from `./jumail dns`.

### TLS without a Cloudflare token

Set `JUMAIL_TLS=selfsigned`, run `./jumail certs` and give `certs/ca.pem` to
the apps (`NODE_EXTRA_CA_CERTS`, `ca_file`, `SSL_CERT_FILE`, ...). ACME is the
default because with a real certificate for `mail.juume.site` the apps need
zero TLS configuration.

## Connecting an app

Join the external network and point the mailer at `mail.juume.site:587`,
STARTTLS, PLAIN or LOGIN auth, with the login printed by `./jumail user add`.

```yaml
# compose.yml of any app on the same docker host
services:
  web:
    environment:
      SMTP_HOST: mail.juume.site
      SMTP_PORT: 587
      SMTP_USER: julinks
      SMTP_PASSWORD: ${SMTP_PASSWORD}
      MAIL_FROM: noreply@juume.site
    networks: [default, jumail]
networks:
  jumail:
    external: true
```

<details><summary>Client snippets</summary>

```js
// Node / nodemailer
nodemailer.createTransport({ host: "mail.juume.site", port: 587, secure: false, requireTLS: true,
  auth: { user: process.env.SMTP_USER, pass: process.env.SMTP_PASSWORD } });
```

```ruby
# Rails
config.action_mailer.smtp_settings = { address: "mail.juume.site", port: 587,
  user_name: ENV["SMTP_USER"], password: ENV["SMTP_PASSWORD"],
  authentication: :plain, enable_starttls: true }
```

```python
# Python
s = smtplib.SMTP("mail.juume.site", 587); s.starttls(context=ssl.create_default_context())
s.login(user, password); s.send_message(msg)
```

```csharp
// .NET / MailKit
await client.ConnectAsync("mail.juume.site", 587, SecureSocketOptions.StartTls);
await client.AuthenticateAsync(user, password);
```
</details>

Containers started by Kamal (Household) are on the `kamal` network; connect
them with `docker network connect jumail <container>` from a post-deploy hook,
or use the host loopback `127.0.0.1:587` with `--add-host`.

## Operating it

| command | |
|---|---|
| `./jumail user add NAME [ADDR...]` | new login, prints the password once |
| `./jumail user passwd NAME` / `rm NAME` / `list` | rotate, revoke, inspect |
| `./jumail dns` / `./jumail dkim` | records to publish |
| `./jumail logs` / `./jumail queue` / `./jumail status` | what is happening |
| `git pull && ./jumail up` | update |

The queue retries with exponential back-off (18 min, 21, 25 ... up to 12
tries) if the provider is down. There are no bounce messages, because there is
no mailbox to deliver them to: failures show up in `./jumail logs`.

Rotating the provider key is one line in `.env` and `./jumail up`; no app
changes. Swapping providers (Mailjet, Resend, SMTP2GO...) is `RELAY_HOST`,
`RELAY_USER`, `RELAY_PASSWORD`, `RELAY_SPF_INCLUDE`.

## Verification

`ci/` runs the whole chain with [Mailpit](https://mailpit.axllent.org/)
playing the provider (STARTTLS + AUTH required) and `ci/smoke.py` asserting
the policy from the outside:

```bash
./ci/gen-certs.sh
docker compose -f compose.yml -f ci/compose.ci.yml up -d --wait jumail mailpit
COMPOSE_FILE=compose.yml:ci/compose.ci.yml ./jumail user add julinks --password julinks-ci-secret --no-senders
COMPOSE_FILE=compose.yml:ci/compose.ci.yml ./jumail user add jupdf --password jupdf-ci-secret --no-senders
docker compose -f compose.yml -f ci/compose.ci.yml run --rm smoke
```

The 12 checks: AUTH hidden before STARTTLS, unauthenticated send refused,
wrong password, From outside the allow-list, foreign sender domain, spoofed
header From, happy path accepted, relayed upstream over STARTTLS with AUTH,
DKIM signature `d=juume.site s=jumail` present, From preserved, one app cannot
impersonate another, rate limit defers with 451. GitHub Actions runs the same
on every push.

![Relayed message with its DKIM signature, seen by the fake provider](docs/mailpit-headers.png)

## Known limits

- Rate limits are global, not per login (maddy scopes limits by IP, sender
  domain or destination, and every app shares the domain).
- The provider sees message contents. That is the trade for inbox delivery
  from a home connection; the relay keeps the keys to the identity.
- Brevo's free plan is 300 mails/day and may add a small footer.

## Layout

```
compose.yml              relay service, hardened; network "jumail"; data volume
config/maddy.conf        the whole policy, 90 lines
config/site-acme.conf    TLS via Let's Encrypt (Cloudflare DNS-01) + upstream relay
config/site-selfsigned.conf  same with certificate files
config/senders           login -> allowed From addresses (managed by ./jumail)
jumail                   operator CLI
ci/                      Mailpit stand-in, smoke test, compose override, cert generator
.github/workflows/ci.yml boots the stack and runs the smoke test
```
