import asyncio
import websockets
import json
import os
from datetime import datetime

clients = {}
chatrooms = {"general": set()}  # Dictionary to store chatrooms and their clients
UPLOAD_DIR = "uploads"

os.makedirs(UPLOAD_DIR, exist_ok=True)

LOG_DIR = "logs"
os.makedirs(LOG_DIR, exist_ok=True)

# Delete all log files on startup
for log_file in os.listdir(LOG_DIR):
    log_file_path = os.path.join(LOG_DIR, log_file)
    if os.path.isfile(log_file_path):
        os.remove(log_file_path)
print("All log files deleted on startup.")

chatroom_logs = {}  # Dictionary to store logs for each chatroom

def save_logs():
    for chatroom, messages in chatroom_logs.items():
        log_file_path = os.path.join(LOG_DIR, f"{chatroom}.log")
        with open(log_file_path, "w") as log_file: # overwrite the history
            log_file.write("\n".join(messages) + "\n")
    print("Logs saved.")

async def send_chatroom_history(websocket, chatroom):
    """Send the chatroom's message history to the user."""
    if chatroom in chatroom_logs:
        history = chatroom_logs[chatroom]
        history_message = json.dumps({
            "type": "chatroom_history",
            "chatroom": chatroom,
            "history": history
        })
        await websocket.send(history_message)

async def handler(websocket):
    try:
        initial_message = await websocket.recv()
        initial_data = json.loads(initial_message)
        username = initial_data.get("username", "Unknown")
        chatroom = initial_data.get("chatroom", "general")

        if chatroom not in chatrooms:
            chatrooms[chatroom] = set()
        chatrooms[chatroom].add(websocket)
        clients[websocket] = {"username": username, "chatroom": chatroom}

        print(f"User {username} connected to {chatroom}")

        # Send chatroom history to the user
        await send_chatroom_history(websocket, chatroom)

        await broadcast_online_users()

        # join_message = json.dumps({
        #     "sender": "System",
        #     "chatroom": chatroom,
        #     "message": f"{username} has joined {chatroom}",
        #     "timestamp": datetime.now().isoformat()
        # })
        # await broadcast_to_chatroom(chatroom, join_message)

        async for raw_message in websocket:
            try:
                # print(raw_message)
                message = json.loads(raw_message)
                type = message.get("type")
                # print(type)
                if type == "switch_chatroom":
                    new_chatroom = message.get("chatroom")
                    if not new_chatroom:
                        continue

                    # Update the client's chatroom
                    old_chatroom = clients[websocket]["chatroom"]
                    if old_chatroom != new_chatroom:
                        chatrooms[old_chatroom].remove(websocket)
                        if not chatrooms[old_chatroom]:
                            del chatrooms[old_chatroom]  # Clean up empty chatrooms

                        if new_chatroom not in chatrooms:
                            chatrooms[new_chatroom] = set()
                        chatrooms[new_chatroom].add(websocket)

                        clients[websocket]["chatroom"] = new_chatroom
                        chatroom = new_chatroom  # Update the local chatroom variable
                        print(f"User {clients[websocket]['username']} switched from {old_chatroom} to {new_chatroom}")

                        # Send the new chatroom's history to the client
                        await send_chatroom_history(websocket, new_chatroom)

                elif type == "upload_file":
                    file_name = message.get("fileName")
                    file_data = message.get("fileData")
                    # print(file_name)
                    # print(file_data)
                    await handle_file_upload(websocket, file_name, file_data, username, chatroom)                    
                elif type == "request_file":
                    await send_file(websocket, message.get("fileName"))
                else:
                    formatted_message = {
                        "sender": username,
                        "chatroom": chatroom,  
                        "message": message.get("message", "").strip(),
                        "timestamp": message.get("timestamp", None) or datetime.now().isoformat()
                    }
                    if not formatted_message["message"]:  # Skip broadcasting if the message is empty
                        print(f"Skipping empty message from {username} in {chatroom}")
                        continue
                    json_data = json.dumps(formatted_message)
                    if "_" in chatroom:  # Flag for DM channel
                        await send_to_dm_channel(chatroom, json_data)
                    else:
                        await broadcast_to_chatroom(chatroom, json_data)
                    print(f"Broadcasting message in {chatroom} from {username}: {formatted_message['message']}")
            except json.JSONDecodeError:
                print("Received invalid JSON")
    except websockets.exceptions.ConnectionClosed:
        pass
    finally:
        user_data = clients.pop(websocket, None)
        if user_data:
            username = user_data["username"]
            chatroom = user_data["chatroom"]
            chatrooms[chatroom].remove(websocket)
            # leave_message = json.dumps({
            #     "sender": "System",
            #     "chatroom": chatroom,
            #     "message": f"{username} has left {chatroom}",
            #     "timestamp": datetime.now().isoformat()
            # })
            # if "_" in chatroom:  # Check if it's a DM channel
            #     await send_to_dm_channel(chatroom, leave_message)
            # else:
            #     await broadcast_to_chatroom(chatroom, leave_message)
            print(f"User {username} disconnected from {chatroom}")
            await broadcast_online_users()

async def broadcast_online_users():
    """Broadcast the list of all online users to all connected clients."""
    online_users = [data["username"] for data in clients.values() if data["username"]]  # Filter out None or invalid usernames
    message = json.dumps({"type": "online_users", "users": online_users})
    await asyncio.gather(*[client.send(message) for client in clients])

async def broadcast_to_chatroom(chatroom, message):
    """Send a message to all clients in a specific chatroom and log it."""
    if chatroom in chatrooms:
        if chatroom not in chatroom_logs:
            chatroom_logs[chatroom] = []
        chatroom_logs[chatroom].append(message)

        # Ensure the chatroom property is included in the message
        message_data = json.loads(message)
        message_data["chatroom"] = chatroom
        updated_message = json.dumps(message_data)

        # Broadcast the message to all clients in the chatroom
        await asyncio.gather(*[client.send(updated_message) for client in chatrooms[chatroom]])

async def handle_file_upload(websocket, file_name, file_data, username, chatroom):
    """Handle file uploads sent as JSON with base64-encoded data."""
    try:
        print("RECEIVED INFO:")
        print(file_name)
        print(file_data)
        if not file_name or not file_data:
            await websocket.send(json.dumps({"status": "error", "message": "Invalid file upload data"}))
            return

        import base64
        decoded_file_data = base64.b64decode(file_data)
        file_path = os.path.join(UPLOAD_DIR, file_name)

        with open(file_path, "wb") as f:
            f.write(decoded_file_data)

        print(f"{datetime.now().isoformat()} - File received and saved as {file_name}")

        response = json.dumps({
            "type": "file_uploaded",
            "chatroom": chatroom,
            "sender": username,
            "filename": file_name,
            "message": f"{username} uploaded a file.",
            "timestamp": datetime.now().isoformat()
        })
        await broadcast_to_chatroom(chatroom, response)
    except Exception as e:
        print(f"Error handling file upload: {e}")
        await websocket.send(json.dumps({"status": "error", "message": "File upload failed"}))

async def send_file(websocket, filename):
    """Send a requested file to the client."""
    print("Rquested file: ", filename)
    file_path = os.path.join(UPLOAD_DIR, filename)

    if not os.path.exists(file_path):
        await websocket.send(json.dumps({"status": "error", "message": "File not found"}))
        return

    try:
        with open(file_path, "rb") as f:
            file_data = f.read()

        import base64
        encoded_file_data = base64.b64encode(file_data).decode("utf-8")

        response = json.dumps({
            "type": "file_download",
            "filename": filename,
            "file_data": encoded_file_data
        })
        await websocket.send(response)
        print(f"File {filename} sent to client")
    except Exception as e:
        print(f"Error sending file: {e}")
        await websocket.send(json.dumps({"status": "error", "message": "File transfer failed"}))

async def send_to_dm_channel(dm_channel, message):
    """Send a message to both users in a DM channel and log it."""
    # Log the message
    if dm_channel not in chatroom_logs:
        chatroom_logs[dm_channel] = []
    chatroom_logs[dm_channel].append(message)

    # Ensure the chatroom property is included in the message
    message_data = json.loads(message)
    message_data["chatroom"] = dm_channel
    updated_message = json.dumps(message_data)

    users_in_channel = dm_channel.split("_")
    for client, data in clients.items():
        if data["username"] in users_in_channel:
            await client.send(updated_message)

async def main():
    try:
        async with websockets.serve(handler, "0.0.0.0", 8765):
            print("WebSocket Server is running on ws://0.0.0.0:8765")
            await asyncio.Future()
    except KeyboardInterrupt:
        print("Server shutting down...")
        save_logs()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Server shutting down...")
        save_logs()
