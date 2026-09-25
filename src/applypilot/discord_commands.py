import time
import httpx
import threading
import logging
from applypilot import config
import os

from applypilot.discord_bot import _headers, _get_dm_channel, _send_message, API_BASE

logger = logging.getLogger(__name__)

_listener_thread = None
pending_question = None

def _listen_loop():
    token = os.environ.get("DISCORD_BOT_TOKEN")
    user_id = os.environ.get("DISCORD_USER_ID")
    if not token or not user_id:
        return

    latest_seen = None
    
    with httpx.Client(headers=_headers(), timeout=15) as client:
        try:
            channel_id = _get_dm_channel(client, user_id)
        except Exception as e:
            logger.error(f"Discord listener failed to get channel: {e}")
            return
            
        # Get the latest message ID to start listening from
        try:
            resp = client.get(f"{API_BASE}/channels/{channel_id}/messages", params={"limit": 1})
            if resp.status_code == 200 and resp.json():
                latest_seen = resp.json()[0]["id"]
        except:
            pass

        while True:
            try:
                params = {"limit": 10}
                if latest_seen:
                    params["after"] = latest_seen
                    
                resp = client.get(f"{API_BASE}/channels/{channel_id}/messages", params=params)
                if resp.status_code == 200:
                    messages = resp.json()
                    
                    for m in reversed(messages):
                        latest_seen = max(latest_seen, m["id"]) if latest_seen else m["id"]
                        
                        if m.get("author", {}).get("id") == user_id:
                            content = m.get("content", "").strip()
                            if content.startswith("!status"):
                                _handle_status(client, channel_id)
                            elif content.startswith("!jobs"):
                                _handle_jobs(client, channel_id)
                            elif content.startswith("!answer "):
                                _handle_answer(client, channel_id, content)
            except Exception as e:
                logger.debug(f"Discord listener error: {e}")
                
            time.sleep(5)

def _handle_answer(client, channel_id, content):
    global pending_question
    if pending_question:
        ans = content.split("!answer ", 1)[1].strip()
        from applypilot.screening import save_answer
        save_answer(pending_question, ans, source="discord")
        _send_message(client, channel_id, f"✅ Saved answer for: *{pending_question[:40]}...*")
        pending_question = None
    else:
        _send_message(client, channel_id, "❌ No pending question to answer right now.")

def _handle_status(client, channel_id):
    from applypilot.database import get_connection
    conn = get_connection()
    
    total = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    p_enrich = conn.execute("SELECT COUNT(*) FROM jobs WHERE detail_scraped_at IS NULL").fetchone()[0]
    p_score = conn.execute("SELECT COUNT(*) FROM jobs WHERE full_description IS NOT NULL AND fit_score IS NULL").fetchone()[0]
    p_tailor = conn.execute("SELECT COUNT(*) FROM jobs WHERE fit_score >= 7 AND full_description IS NOT NULL AND tailored_resume_path IS NULL").fetchone()[0]
    
    queue = conn.execute("SELECT COUNT(*) FROM jobs WHERE tailored_resume_path IS NOT NULL AND apply_status IS NULL").fetchone()[0]
    applied = conn.execute("SELECT COUNT(*) FROM jobs WHERE apply_status = 'applied'").fetchone()[0]
    failed = conn.execute("SELECT COUNT(*) FROM jobs WHERE apply_status = 'failed'").fetchone()[0]
    
    msg = (
        f"**Pipeline Status:**\n"
        f"🔍 Total Discovered: {total}\n"
        f"📄 Pending Enrich: {p_enrich}\n"
        f"⚖️ Pending Score: {p_score}\n"
        f"✍️ Pending Tailor: {p_tailor}\n"
        f"──────────────\n"
        f"⏳ Ready to Apply: {queue}\n"
        f"✅ Applied Successfully: {applied}\n"
        f"❌ Failed Applications: {failed}"
    )
    _send_message(client, channel_id, msg)

def _handle_jobs(client, channel_id):
    from applypilot.database import get_connection
    conn = get_connection()
    jobs = conn.execute("SELECT title, site FROM jobs WHERE apply_status = 'applied' ORDER BY applied_at DESC LIMIT 5").fetchall()
    if not jobs:
        _send_message(client, channel_id, "No jobs applied yet.")
    else:
        msg = "**Recent Applications:**\n" + "\n".join(f"- {j['title']} ({j['site']})" for j in jobs)
        _send_message(client, channel_id, msg)

def start_listener():
    global _listener_thread
    config.load_env()
    if os.environ.get("DISCORD_BOT_TOKEN") and os.environ.get("DISCORD_USER_ID"):
        if _listener_thread is None or not _listener_thread.is_alive():
            _listener_thread = threading.Thread(target=_listen_loop, daemon=True)
            _listener_thread.start()
            logger.info("Started Discord command listener (!status, !jobs)")
