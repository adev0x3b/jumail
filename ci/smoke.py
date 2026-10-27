"""End-to-end smoke test for the JuMail relay.

Runs inside the `smoke` service of ci/compose.ci.yml: talks to the relay on
mail.juume.site:587 and checks what Mailpit (the fake upstream) received.
"""
import email.message
import json
import os
import smtplib
import ssl
import sys
import time
import urllib.request

HOST = os.environ.get("JUMAIL_HOSTNAME", "mail.juume.site")
PORT = 587
MAILPIT = "http://mailpit:8025"
GOOD = ("julinks", "julinks-ci-secret")

failures = []


def check(name, ok, detail=""):
    print(("ok   " if ok else "FAIL ") + name + (f"  ({detail})" if detail else ""))
    if not ok:
        failures.append(name)


def tls_context():
    # The relay uses a throwaway self-signed certificate in CI.
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def connect(starttls=True):
    s = smtplib.SMTP(HOST, PORT, timeout=20)
    s.ehlo("smoke.ci")
    if starttls:
        s.starttls(context=tls_context())
        s.ehlo("smoke.ci")
    return s


def message(frm, to="someone@example.com", subject="JuMail smoke"):
    m = email.message.EmailMessage()
    m["From"] = frm
    m["To"] = to
    m["Subject"] = subject
    m.set_content("Sent through the JuMail relay.")
    return m


def try_send(s, frm, to="someone@example.com", subject="JuMail smoke"):
    try:
        s.send_message(message(frm, to, subject))
        return 250, "accepted"
    except smtplib.SMTPResponseException as e:
        return e.smtp_code, e.smtp_error.decode(errors="replace")
    except smtplib.SMTPRecipientsRefused as e:
        code, msg = next(iter(e.recipients.values()))
        return code, msg.decode(errors="replace")


def wait_for_relay():
    for _ in range(60):
        try:
            connect(starttls=False).quit()
            return
        except OSError:
            time.sleep(1)
    print("relay never answered on %s:%d" % (HOST, PORT))
    sys.exit(2)


def mailpit(path):
    with urllib.request.urlopen(MAILPIT + path, timeout=10) as r:
        return json.load(r)


def wait_for_mailpit():
    for _ in range(60):
        try:
            mailpit("/api/v1/messages?limit=1")
            return
        except OSError:
            time.sleep(1)
    print("mailpit API never answered")
    sys.exit(2)


wait_for_relay()
wait_for_mailpit()
urllib.request.urlopen(urllib.request.Request(MAILPIT + "/api/v1/messages", method="DELETE"), timeout=10)

# 1. No AUTH before STARTTLS: credentials can never travel in clear text.
s = connect(starttls=False)
check("AUTH not advertised before STARTTLS", not s.has_extn("auth"), str(s.esmtp_features))
s.quit()

# 2. Sending without authentication is refused.
s = connect()
code, msg = try_send(s, "julinks@juume.site")
check("unauthenticated send refused", code in (502, 530, 550, 553), f"{code} {msg}")
s.quit()

# 3. Wrong password.
s = connect()
try:
    s.login(GOOD[0], "wrong")
    check("wrong password refused", False, "login succeeded")
except smtplib.SMTPAuthenticationError as e:
    check("wrong password refused", e.smtp_code == 535, f"{e.smtp_code}")
s.quit()

# 4. Right login, From address the app is not allowed to use.
s = connect()
s.login(*GOOD)
code, msg = try_send(s, "admin@juume.site")
check("From not in the app's allow-list refused", code == 553, f"{code} {msg}")
s.quit()

# 5. Right login, foreign sender domain.
s = connect()
s.login(*GOOD)
code, msg = try_send(s, "julinks@example.com")
check("foreign sender domain refused", code in (501, 553), f"{code} {msg}")
s.quit()

# 6. Header From must match the envelope / allow-list too.
s = connect()
s.login(*GOOD)
try:
    m = message("julinks@juume.site")
    del m["From"]
    m["From"] = "ceo@juume.site"
    s.sendmail("julinks@juume.site", ["someone@example.com"], m.as_bytes())
    check("spoofed From header refused", False, "accepted")
except smtplib.SMTPResponseException as e:
    check("spoofed From header refused", e.smtp_code == 553, f"{e.smtp_code} {e.smtp_error!r}")
s.quit()

# 7. The happy path, with an alias address, then verify at the upstream.
s = connect()
s.login(*GOOD)
code, msg = try_send(s, "noreply@juume.site", subject="JuMail happy path")
check("authenticated send accepted", code == 250, f"{code} {msg}")
s.quit()

msgs = []
for _ in range(60):
    msgs = mailpit("/api/v1/messages?limit=10")["messages"]
    if msgs:
        break
    time.sleep(1)
check("message relayed to upstream over STARTTLS with AUTH", len(msgs) == 1, f"{len(msgs)} messages")
if msgs:
    headers = mailpit(f"/api/v1/message/{msgs[0]['ID']}/headers")
    dkim = headers.get("Dkim-Signature") or headers.get("DKIM-Signature") or []
    sig = dkim[0] if dkim else ""
    check("DKIM signed for juume.site with selector jumail",
          "d=juume.site" in sig and "s=jumail" in sig, sig[:120])
    frm = headers.get("From", [""])[0]
    check("From preserved", "noreply@juume.site" in frm, frm)

# 8. Second app cannot use the first app's address.
s = connect()
s.login("jupdf", "jupdf-ci-secret")
code, msg = try_send(s, "julinks@juume.site")
check("apps cannot impersonate each other", code == 553, f"{code} {msg}")
s.quit()

# 9. Global rate limit (CI runs with burst 20 per minute, shared by every
# attempt above, so just send until the relay starts deferring).
s = connect()
s.login(*GOOD)
codes = []
for i in range(30):
    code, msg = try_send(s, "julinks@juume.site", subject=f"burst {i}")
    codes.append(code)
    if code == 451:
        break
check("rate limit defers with 451 after the burst",
      250 in codes and codes[-1] == 451 and all(c in (250, 451) for c in codes),
      f"{len(codes)} sends, last={codes[-1]}")
s.quit()

print()
if failures:
    print(f"{len(failures)} check(s) failed: {', '.join(failures)}")
    sys.exit(1)
print("all checks passed")
