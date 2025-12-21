import asyncio
import os
import sys
from telethon import TelegramClient, utils
from telethon.tl.functions.chatlists import CheckChatlistInviteRequest, JoinChatlistInviteRequest
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Configuration for Account 16
# We try to find specific config or fall back to global
SESSION_NAME = "account_16"
SESSION_DIR = os.getenv("SESSION_DIR", "sessions")
SESSION_PATH = os.path.join(SESSION_DIR, SESSION_NAME)

API_ID = os.getenv(f"TG_{SESSION_NAME}_API_ID") or os.getenv("TG_API_ID")
API_HASH = os.getenv(f"TG_{SESSION_NAME}_API_HASH") or os.getenv("TG_API_HASH")

if not API_ID or not API_HASH:
    print("Error: Could not find API_ID/API_HASH for account_16 in .env")
    # Fallback to hardcoded if necessary (User didn't provide them in chat, relying on .env)
    sys.exit(1)

API_ID = int(API_ID)

URLS = []

async def main():
    print(f"Connecting to session {SESSION_PATH}...")
    async with TelegramClient(SESSION_PATH, API_ID, API_HASH) as client:
        me = await client.get_me()
        if not me:
            print("Error: Not authorized. Session might be invalid.")
            return
        print(f"Logged in as: {me.first_name} ({me.id})")

        for url in URLS:
            try:
                slug = url.split("addlist/")[-1]
                print(f"\nProcessing slug: {slug}")
                
                # Check the invite
                try:
                    chatlist = await client(CheckChatlistInviteRequest(slug=slug))
                except Exception as e:
                    print(f"Failed to check invite {slug}: {e}")
                    continue

                title = getattr(chatlist, 'title', 'Unknown')
                peers = chatlist.peers
                print(f"Folder: '{title}' contains {len(peers)} chats/channels.")
                
                # Prepare peers to join
                peers_to_join = []
                for peer in peers:
                    # Resolve peer to InputPeer using the chats/users lists returned in chatlist
                    entity = None
                    peer_id = None
                    if hasattr(peer, 'channel_id'):
                        peer_id = peer.channel_id
                    elif hasattr(peer, 'chat_id'):
                        peer_id = peer.chat_id
                    elif hasattr(peer, 'user_id'):
                        peer_id = peer.user_id
                    
                    # Search in chats
                    for c in chatlist.chats:
                        if c.id == peer_id:
                            entity = c
                            break
                    # Search in users if not found
                    if not entity:
                        for u in chatlist.users:
                            if u.id == peer_id:
                                entity = u
                                break
                    
                    if entity:
                        try:
                            peers_to_join.append(utils.get_input_peer(entity))
                        except Exception as e:
                            print(f"Skipping peer {peer_id}: {e}")
                
                if not peers_to_join:
                    print("No joinable peers found.")
                    continue

                print(f"Attempting to join {len(peers_to_join)} chats...")
                await client(JoinChatlistInviteRequest(slug=slug, peers=peers_to_join))
                print(f"Successfully joined folder: {title}")

            except Exception as e:
                print(f"Error processing {url}: {e}")

if __name__ == "__main__":
    asyncio.run(main())
