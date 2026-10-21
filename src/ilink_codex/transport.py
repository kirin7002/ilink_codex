"""Small SDK adapter: no SDK ACL defaults or in-memory cursor/token dependency."""
from __future__ import annotations

from urllib.parse import urlsplit


class TransportError(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(f"iLink error {code}")


def check_response(response: dict):
    for key in ('ret', 'errcode'):
        code = response.get(key, 0)
        if code not in (None, 0):
            raise TransportError(code)


class ILinkTransport:
    def __init__(self, credentials: dict):
        from weixin_ilink.api import ClientOptions
        url = urlsplit(credentials['base_url'])
        if url.scheme != 'https' or not url.hostname or url.username or url.password:
            raise ValueError("iLink base_url must be HTTPS without embedded credentials")
        self.account = credentials['account_id']
        self.options = ClientOptions(base_url=credentials['base_url'], token=credentials['bot_token'])

    def poll(self, cursor: str):
        from weixin_ilink.api import get_updates
        response = get_updates(self.options, cursor)
        check_response(response)
        return response

    def send(self, user: str, text: str, context: str, client_id: str):
        from weixin_ilink.api import send_message
        response = send_message(self.options, {
            'from_user_id': '', 'to_user_id': user, 'client_id': client_id,
            'message_type': 2, 'message_state': 2, 'context_token': context,
            'item_list': [{'type': 1, 'text_item': {'text': text}}],
        })
        check_response(response)
