import argparse

import uvicorn

parser = argparse.ArgumentParser(description="Inicia Orca Metadata Inspector en la interfaz local.")
parser.add_argument("--port", type=int, default=8000)
args = parser.parse_args()
if not 1 <= args.port <= 65535:
    parser.error("El puerto debe estar entre 1 y 65535.")
uvicorn.run("orca.main:app", host="127.0.0.1", port=args.port, proxy_headers=False)
