"""Shared MIME assembly for backends that emit a raw RFC 5322 message.

SMTP hands the built message to `smtplib.send_message`; SES sends its bytes
as `Raw` content. Kept in one place so the multipart structure — and the CID
wiring for inline images — stays identical across backends.
"""

from email.message import EmailMessage as MimeMessage
from typing import cast

from app.core.email.backends.base import EmailMessage


def build_mime(message: EmailMessage) -> MimeMessage:
    """Assemble `message` into a stdlib MIME object: text + HTML + inline images.

    Inline images attach to the HTML alternative as `multipart/related` parts,
    so a `cid:<cid>` reference in `body_html` resolves in the client. With no
    images the result is a plain `multipart/alternative`.
    """
    mime = MimeMessage()
    mime["Subject"] = message.subject
    mime["From"] = message.from_addr
    mime["To"] = message.to
    mime.set_content(message.body_text)
    mime.add_alternative(message.body_html, subtype="html")

    if message.inline_images:
        # add_alternative above guarantees the html part exists; cast drops the None arm.
        html_part = cast("MimeMessage", mime.get_body(preferencelist=("html",)))
        for image in message.inline_images:
            html_part.add_related(image.data, maintype="image", subtype=image.subtype, cid=f"<{image.cid}>")

    return mime
