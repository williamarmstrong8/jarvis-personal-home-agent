#!/usr/bin/env python3
import os, http.server, socketserver

PORT = int(os.environ.get("PORT", 3333))
os.chdir(os.path.dirname(os.path.abspath(__file__)))

Handler = http.server.SimpleHTTPRequestHandler
with socketserver.TCPServer(("", PORT), Handler) as httpd:
    print(f"Serving ui/ on port {PORT}")
    httpd.serve_forever()
