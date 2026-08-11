"""Proves /chat and /workflow-chat are genuinely separate: different system
prompts, different session/history buckets, same underlying session/db
mechanics (chat_log) -- exactly what was asked for. No LLM server needed:
orchestrator.default_llm_complete is monkeypatched to a fake that just
records which system prompt it was given and returns a plain reply, so this
tests the REAL HTTP routes/handlers, not a hand-rolled substitute.
"""
from __future__ import annotations

import importlib
import os
import shutil
import sys
from pathlib import Path

from starlette.testclient import TestClient

ROOT = Path(__file__).parent.parent.resolve()
TMP = ROOT / "_smoke" / ".tmp" / "chatbot-separation"
shutil.rmtree(TMP, ignore_errors=True)
TMP.mkdir(parents=True, exist_ok=True)

os.environ.update({
    "GOVERNANCE_ADMIN_USER": "sep_admin",
    "GOVERNANCE_ADMIN_PASSWORD": "sep_password",
    "GOVERNANCE_SESSION_SECRET": "chatbot-separation-secret",
    "GOVERNANCE_COOKIE_SECURE": "false",
    "GOVERNANCE_ARTIFACT_DIR": str(TMP / "artifacts"),
    "GOVERNANCE_STATE_DIR": str(TMP / "state"),
    "GOVERNANCE_STORE_FILE": str(TMP / "policy-store.json"),
})

PASS, FAIL = 0, 0


def check(name, cond, detail=None):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}", detail if detail is not None else "")


sys.path.insert(0, str(ROOT / "gateway"))
sys.path.insert(0, str(ROOT / "governance_core"))
gateway_app = importlib.import_module("app")
import orchestrator  # noqa: E402
from store import get_store  # noqa: E402
from store.models import ConsumerRecord  # noqa: E402
from auth.passwords import hash_password  # noqa: E402

store = get_store()
store.upsert_consumer(ConsumerRecord(
    consumer_id="user:sep_tester", name="sep_tester", key_hash="", status="active",
    role="user", type="user", categories=[],
    login_password_hash=hash_password("tester_password"),
))

captured_system_prompts: list[str] = []


class _FakeMsg:
    tool_calls = None

    def __init__(self, content):
        self.content = content


def fake_complete(messages, tools):
    captured_system_prompts.append(messages[0]["content"])
    return _FakeMsg("ok, got it")


orchestrator.default_llm_complete = lambda: fake_complete

with TestClient(gateway_app.app, base_url="http://testserver") as client:
    client.post("/dashboard/login", json={"username": "sep_tester", "password": "tester_password"})

    home_resp = client.post("/chat", json={"message": "hello from home"})
    check("/chat responds", home_resp.status_code == 200, home_resp.text)
    home_body = home_resp.json()
    home_conv_id = home_body.get("conversation_id")

    wf_resp = client.post("/workflow-chat", json={"message": "hello from workflow copilot"})
    check("/workflow-chat responds", wf_resp.status_code == 200, wf_resp.text)
    wf_body = wf_resp.json()
    wf_conv_id = wf_body.get("conversation_id")

    check("exactly 2 turns captured (one per endpoint)", len(captured_system_prompts) == 2, captured_system_prompts)
    home_prompt, wf_prompt = captured_system_prompts

    check("/chat used the general SYSTEM_PROMPT", home_prompt == orchestrator.SYSTEM_PROMPT, home_prompt[:200])
    check("/workflow-chat used the dedicated WORKFLOW_COPILOT_SYSTEM_PROMPT",
          wf_prompt == orchestrator.WORKFLOW_COPILOT_SYSTEM_PROMPT, wf_prompt[:200])
    check("the two prompts are actually different", home_prompt != wf_prompt)
    check("home prompt does NOT mention the workflow-building contract",
          "submit_workflow_request" not in home_prompt, home_prompt)
    check("workflow prompt DOES mention the workflow-building contract",
          "submit_workflow_request" in wf_prompt, wf_prompt)

    check("conversation ids differ between the two chatbots", home_conv_id != wf_conv_id, (home_conv_id, wf_conv_id))
    check("workflow conversation id is in its own namespace", wf_conv_id.startswith("workflow-chat:"), wf_conv_id)
    check("home conversation id uses the original namespace", home_conv_id.startswith("chat:"), home_conv_id)

    # ── Both go through the SAME session/db storage mechanics (chat_log) --
    #    same history endpoints work for either, unmodified. ─────────────────
    home_hist = client.get(f"/dashboard/chat-history/{home_conv_id}")
    check("home session is readable via the shared chat-history endpoint", home_hist.status_code == 200, home_hist.text)
    check("home session's transcript has the right content",
          any("hello from home" in m.get("content", "") for m in home_hist.json().get("messages", [])), home_hist.json())

    wf_hist = client.get(f"/dashboard/chat-history/{wf_conv_id}")
    check("workflow session is ALSO readable via the exact same shared endpoint", wf_hist.status_code == 200, wf_hist.text)
    check("workflow session's transcript has the right content, not the home one's",
          any("hello from workflow copilot" in m.get("content", "") for m in wf_hist.json().get("messages", [])), wf_hist.json())

    # ── A second home turn continues in the SAME home session -- unaffected ──
    home_resp2 = client.post("/chat", json={"message": "second home message", "conversation_id": home_conv_id})
    check("second /chat turn succeeds and reuses the same session",
          home_resp2.status_code == 200 and home_resp2.json().get("conversation_id") == home_conv_id, home_resp2.text)
    check("home system prompt stayed the same across turns", captured_system_prompts[-1] == orchestrator.SYSTEM_PROMPT)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
