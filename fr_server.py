#!/usr/bin/env python3
"""
Frisson WebSocket Relay Server
Bridges the Frisson webapp (Web Bluetooth) and the ChillsDemo app.
Based on fr_server.py from E4002 (Abhi, MIT Media Labs).

The Frisson webapp connects as a WebSocket client. Our ChillsDemo app
also connects as a client. When ChillsDemo sends "trigger_device",
this server broadcasts it to the webapp, which fires the haptic device
over Bluetooth.
"""

import asyncio
import json
import signal
import websockets

connected = set()


async def handler(websocket):
    connected.add(websocket)
    print(f"[Server] Client connected  ({len(connected)} total)")
    await broadcast_status()
    try:
        async for message in websocket:
            if message == "trigger_device":
                targets = connected - {websocket}
                for client in targets:
                    try:
                        await client.send("trigger_device")
                    except websockets.exceptions.ConnectionClosed:
                        pass
            elif message == "status":
                await websocket.send(json.dumps({
                    "type": "status",
                    "clients": len(connected),
                }))
            elif message == "ping":
                await websocket.send("pong")
            else:
                # Relay any other message to all other clients
                targets = connected - {websocket}
                for client in targets:
                    try:
                        await client.send(message)
                    except websockets.exceptions.ConnectionClosed:
                        pass
    except websockets.exceptions.ConnectionClosed:
        pass
    finally:
        connected.discard(websocket)
        print(f"[Server] Client disconnected ({len(connected)} total)")
        await broadcast_status()


async def broadcast_status():
    """Notify all clients of the current connection count."""
    msg = json.dumps({"type": "status", "clients": len(connected)})
    for client in connected.copy():
        try:
            await client.send(msg)
        except Exception:
            connected.discard(client)


async def main():
    print("[Server] Frisson relay starting on ws://localhost:8766")
    loop = asyncio.get_running_loop()
    stop = loop.create_future()

    # Graceful shutdown on SIGTERM / SIGINT
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set_result, None)
        except NotImplementedError:
            pass  # Windows doesn't support add_signal_handler

    async with websockets.serve(handler, "localhost", 8766):
        print("[Server] Ready — waiting for connections")
        await stop


if __name__ == "__main__":
    asyncio.run(main())
