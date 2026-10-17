"""
askadma — an in-page AI agent for ADMA.
Backend-switchable: NRP (OpenAI-compatible) or Anthropic, set by AGENT_BACKEND.
Tools are backed by ADMA's database/models: navigate, search, read, open.
"""
import os
import json
import csv

from django.http import JsonResponse
from django.views.decorators.http import require_POST
from django.contrib.auth.decorators import login_required

from .models import Folder, File

BACKEND = os.environ.get("AGENT_BACKEND", "nrp")
NRP_MODEL = "glm-5"
ANTHROPIC_MODEL = "claude-haiku-4-5-20251001"
MAX_STEPS = 8


# ---------- Tool functions (unchanged logic) ----------

def tool_list_folders():
    folders = Folder.objects.filter(parent=None).order_by("name")
    return json.dumps([
        {"name": f.name, "is_third_party": f.is_third_party,
         "subfolders": f.subfolders.count(), "files": f.files.count()}
        for f in folders
    ])


def tool_list_folder_contents(folder_name):
    folder = Folder.objects.filter(name__iexact=folder_name).first()
    if not folder:
        return json.dumps({"error": f"No folder named '{folder_name}' found."})
    return json.dumps({
        "folder": folder.name,
        "subfolders": [sf.name for sf in folder.subfolders.all().order_by("name")],
        "files": [{"name": fi.name, "type": fi.file_type, "size": fi.file_size}
                  for fi in folder.files.all().order_by("name")],
    })


def tool_read_file_info(file_name):
    files = File.objects.filter(name__iexact=file_name)[:5]
    if not files:
        return json.dumps({"error": f"No file named '{file_name}' found."})
    return json.dumps([
        {"name": fi.name, "type": fi.file_type, "size": fi.file_size,
         "location": fi.folder.get_full_path() if fi.folder else "(root)"}
        for fi in files
    ])


def tool_read_file_data(file_name, max_rows=50):
    fi = File.objects.filter(name__iexact=file_name).first()
    if not fi:
        return json.dumps({"error": f"No file named '{file_name}' found."})
    if fi.file_type not in ("csv", "spreadsheet"):
        return json.dumps({"error": f"File '{file_name}' is type '{fi.file_type}', not CSV/spreadsheet."})
    try:
        if fi.file_type == "csv":
            with fi.open_content("r") as fh:
                reader = csv.reader(fh)
                rows = [row for i, row in enumerate(reader) if i <= max_rows]
            return json.dumps({"columns": rows[0] if rows else [], "rows": rows[1:],
                               "note": f"Up to {max_rows} rows."})
        else:
            import pandas as pd
            with fi.open_content("rb") as fh:
                df = pd.read_excel(fh).head(max_rows)
            return json.dumps({"columns": list(df.columns.astype(str)),
                               "rows": df.astype(str).values.tolist(),
                               "note": f"Up to {max_rows} rows."})
    except Exception as e:
        return json.dumps({"error": f"Could not read '{file_name}': {e}"})


def tool_search(query):
    folders = Folder.objects.filter(name__icontains=query)[:20]
    files = File.objects.filter(name__icontains=query)[:30]
    results = {
        "folders": [{"name": f.name, "path": f.get_full_path()} for f in folders],
        "files": [{"name": fi.name, "type": fi.file_type,
                   "location": fi.folder.get_full_path() if fi.folder else "(root)"} for fi in files],
    }
    if not results["folders"] and not results["files"]:
        return json.dumps({"message": f"Nothing found matching '{query}'."})
    return json.dumps(results)


def tool_open_file(file_name):
    fi = File.objects.filter(name__iexact=file_name).first()
    if not fi:
        return json.dumps({"error": f"No file named '{file_name}' found."})
    return json.dumps({"ui_action": {"type": "navigate", "url": f"/file/{fi.id}/"},
                       "message": f"Opening {fi.name}."})


def tool_open_folder(folder_name):
    folder = Folder.objects.filter(name__iexact=folder_name).first()
    if not folder:
        return json.dumps({"error": f"No folder named '{folder_name}' found."})
    return json.dumps({"ui_action": {"type": "navigate", "url": f"/folder/{folder.id}/"},
                       "message": f"Opening the {folder.name} folder."})




def tool_summarize_file(file_name):
    fi = File.objects.filter(name__iexact=file_name).first()
    if not fi:
        return json.dumps({"error": "No file named that found."})
    if fi.file_type in ("csv", "spreadsheet"):
        return tool_read_file_data(file_name, max_rows=30)
    if fi.file_type == "text":
        try:
            with fi.open_content("r") as fh:
                text = fh.read(4000)
            return json.dumps({"name": fi.name, "text_preview": text})
        except Exception as e:
            return json.dumps({"error": "Could not read file."})
    return json.dumps({"name": fi.name, "type": fi.file_type, "note": "No preview for this type."})


def tool_get_context(current_url=""):
    import re
    if not current_url:
        return json.dumps({"note": "No current page context."})
    m = re.search(r"/folder/([0-9a-f-]+)/", current_url)
    if m:
        folder = Folder.objects.filter(id=m.group(1)).first()
        if folder:
            return json.dumps({"viewing": "folder", "name": folder.name, "path": folder.get_full_path()})
    m = re.search(r"/file/([0-9a-f-]+)/", current_url)
    if m:
        fi = File.objects.filter(id=m.group(1)).first()
        if fi:
            return json.dumps({"viewing": "file", "name": fi.name, "type": fi.file_type})
    return json.dumps({"viewing": "page", "url": current_url})




def tool_read_image(file_name, question="Describe this image in detail."):
    import base64, io
    from PIL import Image
    fi = File.objects.filter(name__iexact=file_name).first()
    if not fi:
        return json.dumps({"error": "No file named that found."})
    if fi.file_type != "image":
        return json.dumps({"error": "That file is not an image."})
    try:
        with fi.open_content("rb") as fh:
            raw = fh.read()
        # Resize down so the vision model gets a clean, bounded image
        img = Image.open(io.BytesIO(raw))
        if img.mode != "RGB":
            img = img.convert("RGB")
        img.thumbnail((1024, 1024))
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=85)
        b64 = base64.b64encode(buf.getvalue()).decode("utf-8")
        from openai import OpenAI
        client = OpenAI(base_url=os.environ["NRP_BASE_URL"], api_key=os.environ["NRP_API_KEY"])
        resp = client.chat.completions.create(
            model="qwen3-vl",
            messages=[{
                "role": "user",
                "content": [
                    {"type": "text", "text": question},
                    {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + b64}},
                ],
            }],
            max_tokens=700,
        )
        desc = resp.choices[0].message.content
        if not desc or not desc.strip():
            return json.dumps({"image": fi.name, "error": "The vision model returned an empty description."})
        return json.dumps({"image": fi.name, "description": desc})
    except Exception as e:
        return json.dumps({"error": "Could not analyze image: " + str(e)})


def tool_plot_data(file_name, x_column, y_column, chart_type="bar"):
    fi = File.objects.filter(name__iexact=file_name).first()
    if not fi:
        return json.dumps({"error": "No file named that found."})
    if fi.file_type not in ("csv", "spreadsheet"):
        return json.dumps({"error": "That file is not a CSV or spreadsheet."})
    try:
        if fi.file_type == "csv":
            with fi.open_content("r") as fh:
                reader = csv.reader(fh)
                rows = [row for row in reader]
            headers = rows[0]
            data_rows = rows[1:200]
        else:
            import pandas as pd
            with fi.open_content("rb") as fh:
                df = pd.read_excel(fh).head(200)
            headers = list(df.columns.astype(str))
            data_rows = df.astype(str).values.tolist()
        if x_column not in headers or y_column not in headers:
            return json.dumps({"error": "Column not found. Available: " + ", ".join(headers)})
        xi = headers.index(x_column)
        yi = headers.index(y_column)
        labels = [r[xi] for r in data_rows]
        values = []
        for r in data_rows:
            try:
                values.append(float(r[yi]))
            except Exception:
                values.append(0)
        return json.dumps({
            "ui_action": {
                "type": "render_chart",
                "chart_type": chart_type,
                "labels": labels,
                "values": values,
                "x_label": x_column,
                "y_label": y_column,
                "title": y_column + " by " + x_column,
            },
            "message": "Here is the " + chart_type + " chart of " + y_column + " by " + x_column + "."
        })
    except Exception as e:
        return json.dumps({"error": "Could not create chart."})


TOOL_FUNCS = {
    "list_folders": lambda a: tool_list_folders(),
    "search": lambda a: tool_search(a.get("query", "")),
    "open_file": lambda a: tool_open_file(a.get("file_name", "")),
    "open_folder": lambda a: tool_open_folder(a.get("folder_name", "")),
    "list_folder_contents": lambda a: tool_list_folder_contents(a.get("folder_name", "")),
    "read_file_info": lambda a: tool_read_file_info(a.get("file_name", "")),
    "read_file_data": lambda a: tool_read_file_data(a.get("file_name", "")),
    "summarize_file": lambda a: tool_summarize_file(a.get("file_name", "")),
    "get_context": lambda a: tool_get_context(a.get("current_url", "")),
    "read_image": lambda a: tool_read_image(a.get("file_name", ""), a.get("question", "Describe this image in detail.")),
    "plot_data": lambda a: tool_plot_data(a.get("file_name", ""), a.get("x_column", ""), a.get("y_column", ""), a.get("chart_type", "bar")),
}


# ---------- Tool schemas (shared properties) ----------

_TOOL_DEFS = [
    ("list_folders", "List the top-level folders in ADMA.", {}, []),
    ("search", "Search for files and folders by name across ADMA. Use when the user wants to find something and you don't know where it is.",
     {"query": {"type": "string", "description": "Text to search for."}}, ["query"]),
    ("open_file", "Open a file's page in the ADMA interface for the user. Use when the user asks to open/view/go to a file.",
     {"file_name": {"type": "string"}}, ["file_name"]),
    ("open_folder", "Open a folder's page in the ADMA interface. Use when the user asks to open/go to a folder.",
     {"folder_name": {"type": "string"}}, ["folder_name"]),
    ("list_folder_contents", "List the subfolders and files inside a folder (by name). Use to navigate into folders.",
     {"folder_name": {"type": "string"}}, ["folder_name"]),
    ("read_file_info", "Find a file by name; return its type, size, and folder location.",
     {"file_name": {"type": "string"}}, ["file_name"]),
    ("read_file_data", "Read a CSV/spreadsheet file's columns and first rows, to analyze or summarize.",
     {"file_name": {"type": "string"}}, ["file_name"]),
    ("summarize_file", 'Read a file (CSV, spreadsheet, or text) and get its contents so you can summarize it.',
     {"file_name": {"type": "string"}}, ["file_name"]),
    ("get_context", "Find out what page, folder, or file the user is currently viewing. Use for requests like 'summarize this' or 'what is in here'.",
     {"current_url": {"type": "string"}}, []),
    ("read_image", 'Look at an image file and describe what is in it. Use when the user asks what is in a photo or to describe/analyze an image.',
     {"file_name": {"type": "string"}, "question": {"type": "string"}}, ["file_name"]),
    ("plot_data", 'Create a chart from a CSV or spreadsheet file. Specify x_column, y_column (exact column names), and chart_type (bar, line, or pie). The chart is shown to the user.',
     {"file_name": {"type": "string"}, "x_column": {"type": "string"}, "y_column": {"type": "string"}, "chart_type": {"type": "string"}}, ["file_name", "x_column", "y_column"]),
]


def openai_tools():
    return [{
        "type": "function",
        "function": {
            "name": name,
            "description": desc,
            "parameters": {"type": "object", "properties": props, "required": req},
        },
    } for (name, desc, props, req) in _TOOL_DEFS]


def _run_tool(name, args):
    func = TOOL_FUNCS.get(name)
    if not func:
        return "Unknown tool", None
    result = func(args)
    action = None
    try:
        parsed = json.loads(result)
        if isinstance(parsed, dict) and parsed.get("ui_action"):
            action = parsed["ui_action"]
    except Exception:
        pass
    return result, action


# ---------- The agent loop (OpenAI / NRP) ----------

def run_agent_openai(history, current_url=""):
    from openai import OpenAI
    client = OpenAI(base_url=os.environ["NRP_BASE_URL"], api_key=os.environ["NRP_API_KEY"])
    messages = list(history)
    if current_url:
        messages.insert(0, {"role": "system",
            "content": "The user is currently viewing this page URL: " + current_url +
                       ". If they say 'this', 'here', or 'current', use get_context with this URL."})
    tools = openai_tools()
    captured_action = None

    for _ in range(MAX_STEPS):
        resp = client.chat.completions.create(
            model=NRP_MODEL, messages=messages, tools=tools, max_tokens=1500,
        )
        msg = resp.choices[0].message

        if msg.tool_calls:
            messages.append({
                "role": "assistant",
                "content": msg.content or "",
                "tool_calls": [
                    {"id": tc.id, "type": "function",
                     "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                    for tc in msg.tool_calls
                ],
            })
            for tc in msg.tool_calls:
                try:
                    args = json.loads(tc.function.arguments or "{}")
                except Exception:
                    args = {}
                result, action = _run_tool(tc.function.name, args)
                if action:
                    captured_action = action
                messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})
            continue

        return (msg.content or ""), captured_action

    return "Sorry, I couldn't complete that in time.", captured_action


def run_agent(history, current_url=""):
    return run_agent_openai(history, current_url)


@login_required
@require_POST
def askadma_view(request):
    try:
        data = json.loads(request.body.decode("utf-8"))
        history = data.get("messages")
        current_url = data.get("current_url", "")
        if not history:
            q = (data.get("question") or "").strip()
            history = [{"role": "user", "content": q}] if q else []
    except Exception:
        return JsonResponse({"error": "Invalid request body"}, status=400)
    if not history:
        return JsonResponse({"error": "No question provided"}, status=400)
    try:
        answer, action = run_agent(history, current_url)
        payload = {"answer": answer}
        if action:
            payload["action"] = action
        return JsonResponse(payload)
    except Exception as e:
        return JsonResponse({"error": str(e)}, status=500)
