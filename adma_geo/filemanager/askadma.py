"""
askadma — a simple in-page AI agent for ADMA.
Receives a user question, lets Claude use tools backed by ADMA's own database
models to answer it, and returns the answer as JSON.
"""
import json
import anthropic

from django.http import JsonResponse
from django.views.decorators.http import require_POST
from django.contrib.auth.decorators import login_required

from .models import Folder

MODEL = "claude-haiku-4-5-20251001"


def tool_list_folders():
    folders = Folder.objects.filter(parent=None).order_by("name")
    out = []
    for f in folders:
        out.append({
            "id": str(f.id),
            "name": f.name,
            "is_public": f.is_public,
            "is_third_party": f.is_third_party,
        })
    return json.dumps(out)


TOOLS = [
    {
        "name": "list_folders",
        "description": "List the top-level folders in ADMA, including the ADAPT third-party folder.",
        "input_schema": {"type": "object", "properties": {}},
    }
]


def run_agent(question):
    client = anthropic.Anthropic()
    messages = [{"role": "user", "content": question}]

    for _ in range(5):
        resp = client.messages.create(
            model=MODEL,
            max_tokens=1024,
            tools=TOOLS,
            messages=messages,
        )

        if resp.stop_reason == "tool_use":
            messages.append({"role": "assistant", "content": resp.content})
            results = []
            for block in resp.content:
                if block.type == "tool_use":
                    if block.name == "list_folders":
                        result = tool_list_folders()
                    else:
                        result = "Unknown tool"
                    results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result,
                    })
            messages.append({"role": "user", "content": results})
            continue

        answer = ""
        for block in resp.content:
            if block.type == "text":
                answer += block.text
        return answer

    return "Sorry, I couldn't complete that request."


@login_required
@require_POST
def askadma_view(request):
    try:
        data = json.loads(request.body.decode("utf-8"))
        question = (data.get("question") or "").strip()
    except Exception:
        return JsonResponse({"error": "Invalid request body"}, status=400)

    if not question:
        return JsonResponse({"error": "No question provided"}, status=400)

    try:
        answer = run_agent(question)
        return JsonResponse({"answer": answer})
    except Exception as e:
        return JsonResponse({"error": str(e)}, status=500)
