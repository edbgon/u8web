#!/bin/bash
python -c "from http.server import ThreadingHTTPServer,SimpleHTTPRequestHandler; ThreadingHTTPServer(('', 5555),SimpleHTTPRequestHandler).serve_forever()"
