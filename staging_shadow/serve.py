"""Loopback-only CI server, no access/error payload logging."""
import os
from werkzeug.serving import make_server, WSGIRequestHandler
from .runtime import make_application


class Quiet(WSGIRequestHandler):
    def log(self, *_args, **_kwargs): pass


def main():
    app = make_application()
    server = make_server('127.0.0.1', int(os.environ.get('PORT', '8080')), app, request_handler=Quiet)
    try: server.serve_forever()
    finally: server.server_close()


if __name__ == '__main__': main()
