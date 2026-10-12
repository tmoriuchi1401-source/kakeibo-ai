"""Explicit CI/Cloud Run binding; unchanged isolated application and logging."""
import os
from werkzeug.serving import make_server, WSGIRequestHandler
from .runtime import make_application
from .config import Config


class Quiet(WSGIRequestHandler):
    def log(self, *_args, **_kwargs): pass


def main():
    config = Config.read(os.environ)  # Validate before application or socket creation.
    app = make_application()
    server = make_server(config.bind_host, config.port, app, request_handler=Quiet)
    try: server.serve_forever()
    finally: server.server_close()


if __name__ == '__main__': main()
