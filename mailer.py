# mailer.py
# Verification emails over SMTP, using only the stdlib.
#
# Configure with environment variables (any provider works, e.g. Resend,
# SendGrid, Mailgun, Brevo, or a Gmail app password):
#   SMTP_HOST       host name, enables real sending when set
#   SMTP_PORT       default 587
#   SMTP_USER       login, optional for local relays
#   SMTP_PASSWORD   password or API key
#   SMTP_FROM       From address, defaults to SMTP_USER
#   SMTP_TLS        "1" (default) uses STARTTLS
#   APP_BASE_URL    public origin for the verification link, e.g. https://suaru.onrender.com
#
# Without SMTP_HOST the email is not sent: the link is printed to the server
# log so the flow still works end to end in development and in a demo.

import logging
import os
import smtplib
import ssl
from email.message import EmailMessage

log = logging.getLogger("suaru.mailer")


def smtp_configured() -> bool:
    return bool(os.getenv("SMTP_HOST"))


def send_verification_email(to_email: str, username: str, verify_url: str) -> bool:
    """Send the verification link. Returns True if SMTP accepted the message.

    Raises nothing: mail problems must not break registration; the caller
    decides what to tell the user.
    """
    if not smtp_configured():
        log.warning("SMTP is not configured; verification link for %s: %s", to_email, verify_url)
        print(f"[suaru] verification link for {to_email}: {verify_url}", flush=True)
        return False

    message = EmailMessage()
    message["Subject"] = "Verify your Suaru email"
    message["From"] = os.getenv("SMTP_FROM") or os.getenv("SMTP_USER", "")
    message["To"] = to_email
    message.set_content(
        f"Hi {username},\n\n"
        f"Confirm your email for Suaru: {verify_url}\n\n"
        "The link is valid for 24 hours. If this was not you, ignore this email.\n"
    )

    try:
        port = int(os.getenv("SMTP_PORT", "587"))
        with smtplib.SMTP(os.getenv("SMTP_HOST"), port, timeout=10) as smtp:
            if os.getenv("SMTP_TLS", "1") == "1":
                smtp.starttls(context=ssl.create_default_context())
            user = os.getenv("SMTP_USER")
            password = os.getenv("SMTP_PASSWORD")
            if user and password:
                smtp.login(user, password)
            smtp.send_message(message)
        return True
    except (OSError, smtplib.SMTPException) as exc:
        log.error("Could not send verification email to %s: %s", to_email, exc)
        return False
