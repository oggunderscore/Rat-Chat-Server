import asyncio
import websockets
import json
import os
from datetime import datetime
import firebase_admin
from firebase_admin import credentials, firestore
import requests
import threading
import time
import base64
import hashlib  # Import hashlib for checksum calculation

clients = {}
chatrooms = {"general": set()}  # Dictionary to store chatrooms and their clients
UPLOAD_DIR = "uploads"

os.makedirs(UPLOAD_DIR, exist_ok=True)

LOG_DIR = "logs"
os.makedirs(LOG_DIR, exist_ok=True)

file_chunks = {}  # Store chunks during upload

# Initialize Firebase Admin SDK
cred_path = os.getenv("FIREBASE_ADMIN_CREDS_PATH")
if not cred_path:
    raise EnvironmentError("FIREBASE_ADMIN_CREDS_PATH environment variable is not set.")
cred = credentials.Certificate(cred_path)
firebase_admin.initialize_app(cred)
db = firestore.client()
print("Firebase Admin SDK initialized. Path: ", cred_path)

# Delete all log files on startup
for log_file in os.listdir(LOG_DIR):
    log_file_path = os.path.join(LOG_DIR, log_file)
    if os.path.isfile(log_file_path):
        os.remove(log_file_path)

chatroom_logs = {}  # Dictionary to store logs for each chatroom

async def send_chatroom_history(websocket, chatroom):
    """Send the chatroom's message history to the user from Firestore."""
    try:
        chatroom_ref = db.collection("chatrooms").document(chatroom)
        chatroom_doc = chatroom_ref.get()

        if chatroom_doc.exists:
            history = chatroom_doc.to_dict().get("messages", [])
        else:
            history = []

        history_message = json.dumps({
            "type": "chatroom_history",
            "chatroom": chatroom,
            "history": history
        })
        await websocket.send(history_message)
    except Exception as e:
        print(f"Error fetching chatroom history from Firestore: {e}")

async def broadcast_typing_status(username, chatroom, is_typing):
    """Broadcast typing status to all clients in a chatroom."""
    message = json.dumps({
        "type": "typing_status",
        "username": username,
        "chatroom": chatroom,
        "is_typing": is_typing
    })
    if chatroom in chatrooms:
        await asyncio.gather(*[client.send(message) for client in chatrooms[chatroom]])

async def send_online_users_to_client(websocket):
    """Send the list of online users to a specific client."""
    online_users = list(set(data["username"] for data in clients.values() if data["username"]))
    message = json.dumps({"type": "online_users", "users": online_users})
    await websocket.send(message)

async def handle_file_upload(websocket, file_name, file_data, username, chatroom):
    """Handle file uploads sent as JSON with base64-encoded data."""
    try:
        print("RECEIVED INFO:")
        print(file_name)
        print(file_data)
        if not file_name or not file_data:
            await websocket.send(json.dumps({"status": "error", "message": "Invalid file upload data"}))
            return

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

async def handle_file_chunk(websocket, message, username, chatroom):
    """Handle incoming file chunks and reassemble them."""
    file_id = message.get("fileId")
    if not file_id:
        return

    if file_id not in file_chunks:
        file_chunks[file_id] = {
            "chunks": {},
            "filename": message.get("fileName"),
            "total_size": message.get("totalSize"),
            "received_size": 0,
            "checksum": message.get("checksum"),  # Store expected checksum
        }

    chunk_data = message.get("chunk")
    offset = message.get("offset", 0)
    is_last_chunk = message.get("isLastChunk", False)

    try:
        # Log the received raw chunk
        print(f"[Debug] Received Raw Chunk at offset {offset}: {chunk_data}")

        # Convert chunk back to bytes
        chunk_data = bytes(chunk_data)

        file_chunks[file_id]["chunks"][offset] = chunk_data
        file_chunks[file_id]["received_size"] += len(chunk_data)

        if is_last_chunk:
            # Reassemble the file in the correct order
            sorted_chunks = sorted(file_chunks[file_id]["chunks"].items())
            complete_file = b"".join(chunk for _, chunk in sorted_chunks)

            # Log the raw reassembled file data
            print(f"[Debug] Raw Reassembled File Data (bytes): {complete_file}")

            # Log the checksum of the reassembled file
            reassembled_checksum = hashlib.sha256(complete_file).hexdigest()
            expected_checksum = file_chunks[file_id]["checksum"]
            print(f"[Debug] Reassembled File Checksum: {reassembled_checksum}")
            print(f"[Debug] Expected File Checksum (from client): {expected_checksum}")

            # Validate checksum
            if reassembled_checksum != expected_checksum:
                raise ValueError("Checksum mismatch. File may be corrupted.")

            # Process the complete file
            await handle_complete_file(
                websocket,
                file_chunks[file_id]["filename"],
                complete_file,
                username,
                chatroom,
                reassembled_checksum  # Pass checksum to handle_complete_file
            )

            del file_chunks[file_id]

            await websocket.send(json.dumps({
                "type": "file_upload_status",
                "status": "success",
                "fileId": file_id
            }))
    except Exception as e:
        print(f"Error handling file chunk: {e}")
        await websocket.send(json.dumps({
            "type": "file_upload_status",
            "status": "error",
            "message": f"Error processing file chunk: {e}",
            "fileId": file_id
        }))

async def handle_complete_file(websocket, filename, file_data, username, chatroom, upload_message=None, checksum=None):
    """Process a complete uploaded file."""
    try:
        file_path = os.path.join(UPLOAD_DIR, filename)
        with open(file_path, "wb") as f:
            f.write(file_data)

        response = json.dumps({
            "type": "file_uploaded",
            "chatroom": chatroom,
            "sender": username,
            "filename": filename,
            "message": upload_message,
            "checksum": checksum,  # Include checksum in the response
            "timestamp": datetime.now().isoformat()
        })
        await broadcast_to_chatroom(chatroom, response)
    except Exception as e:
        print(f"Error saving file: {e}")
        raise

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

        # Send online users only to the newly connected client
        await send_online_users_to_client(websocket)

        async for raw_message in websocket:
            try:
                message = json.loads(raw_message)
                type = message.get("type")
                
                print(f"Message: {message} | Type: {type}")
                if type == "ping":
                    username = message.get("username", "Unknown")
                    print(f"Ping received from {username}")
                    await websocket.send(json.dumps({
                        "type": "pong",
                        "username": username
                    }))
                    print(f"Pong sent to {username}")
                    continue

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
                    await handle_file_upload(websocket, file_name, file_data, username, chatroom)
                elif type == "upload_file_chunk":
                    await handle_file_chunk(websocket, message, username, chatroom)
                    continue
                elif type == "request_file":
                    await send_file(websocket, message.get("fileName"))
                elif type == "typing_status":
                    await broadcast_typing_status(
                        username,
                        message.get("chatroom", chatroom),
                        message.get("is_typing", False)
                    )
                    continue
                else:
                    formatted_message = {
                        "sender": username,
                        "chatroom": chatroom,  
                        "message": message.get("message", "").strip(),
                        "timestamp": message.get("timestamp", None) or datetime.now().isoformat(),
                        "isEncrypted": message.get("isEncrypted", False)  # Default to False
                    }
                    if not formatted_message["message"]:  # Skip broadcasting if the message is empty
                        continue
                    json_data = json.dumps(formatted_message)
                    print(f"COOKED message: {json_data}")
                    if "_" in chatroom:  # Flag for DM channel
                        print(f"Broadcasting DM message in {chatroom} from {username}: {formatted_message['message']}")
                        await send_to_dm_channel(chatroom, json_data)
                    else:
                        if chatroom:  # Only send if chatroom is not None or undefined
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
            print(f"User {username} disconnected from {chatroom}")

async def broadcast_to_chatroom(chatroom, message):
    """Send a message to all clients in a specific chatroom and log it to Firestore."""
    if chatroom in chatrooms:
        try:
            # Parse the message to ensure the isEncrypted flag is preserved
            message_data = json.loads(message)
            if "isEncrypted" not in message_data:
                message_data["isEncrypted"] = False  # Default to False if not provided

            updated_message = json.dumps(message_data)

            # Log the message to Firestore immediately
            chatroom_ref = db.collection("chatrooms").document(chatroom)
            chatroom_ref.set({
                "messages": firestore.ArrayUnion([updated_message])
            }, merge=True)

            # Broadcast the message to all clients in the chatroom
            await asyncio.gather(*[client.send(updated_message) for client in chatrooms[chatroom]])
        except Exception as e:
            print(f"Error logging message to Firestore: {e}")

async def send_file(websocket, filename):
    """Send a requested file to the client."""
    file_path = os.path.join(UPLOAD_DIR, filename)

    if not os.path.exists(file_path):
        await websocket.send(json.dumps({"status": "error", "message": "File not found on server"}))
        return

    try:
        with open(file_path, "rb") as f:
            file_data = f.read()

        checksum = hashlib.sha256(file_data).hexdigest()  # Calculate checksum
        encoded_file_data = base64.b64encode(file_data).decode("utf-8")

        response = json.dumps({
            "type": "file_download",
            "filename": filename,
            "file_data": encoded_file_data,
            "checksum": checksum  # Include checksum in the response
        })
        await websocket.send(response)
    except Exception as e:
        print(f"Error sending file: {e}")
        await websocket.send(json.dumps({"status": "error", "message": "File transfer failed"}))

async def send_to_dm_channel(dm_channel, message):
    """Send a message to both users in a DM channel and log it."""
    # Add isEncrypted property to the message
    message_data = json.loads(message)
    message_data["isEncrypted"] = True  # Mark as encrypted for DM messages
    updated_message = json.dumps(message_data)

    # Log the message to Firestore immediately
    try:
        chatroom_ref = db.collection("chatrooms").document(dm_channel)
        chatroom_ref.set({
            "messages": firestore.ArrayUnion([updated_message])
        }, merge=True)
    except Exception as e:
        print(f"Error logging DM message to Firestore: {e}")

    # Ensure the chatroom property is included in the message
    message_data["chatroom"] = dm_channel
    updated_message = json.dumps(message_data)

    users_in_channel = dm_channel.split("_")
    for client, data in clients.items():
        if data["username"] in users_in_channel:
            await client.send(updated_message)

def send_heartbeat():
    PUSH_URL = "https://uptime-kuma-production-4845.up.railway.app/api/push/H6ljqOI2BR?status=up&msg=OK&ping="
    while True:
        try:
            requests.get(PUSH_URL)
            print("Heartbeat sent to Uptime Kuma")
        except Exception as e:
            print("Failed to send heartbeat:", e)
        time.sleep(60)

async def main():
    try:
        # Start the heartbeat thread
        heartbeat_thread = threading.Thread(target=send_heartbeat, daemon=True)
        heartbeat_thread.start()
        
        async with websockets.serve(handler, "0.0.0.0", 8765):
            print("WebSocket Server is running on ws://0.0.0.0:8765")
            await asyncio.Future()
    except KeyboardInterrupt:
        print("Server shutting down...")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Server shutting down...")
